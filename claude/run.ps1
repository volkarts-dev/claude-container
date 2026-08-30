#!/usr/bin/env pwsh
<#
.SYNOPSIS
Starts claude in a container.

.DESCRIPTION
The current directory is always mounted and used as the working directory. Each
additional path given is mounted read-write under /workspace, named after the
last component of that path.

The container runs on an internal Docker network with no route off the host. Its
only way out is a relay container that forwards to the external HTTP proxy named
by -Proxy (or CLAUDE_PROXY / HTTPS_PROXY / HTTP_PROXY). Without a proxy the
container has no network access at all.

.EXAMPLE
./run.ps1 ..\other-repo C:\data -ClaudeArgs --resume
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Paths,
    [string[]]$ClaudeArgs,
    [string]$Image = $(if ($env:CLAUDE_IMAGE) { $env:CLAUDE_IMAGE } else { 'claude-dev' }),
    [string]$Tag = $(if ($env:CLAUDE_TAG) { $env:CLAUDE_TAG } else { 'latest' }),
    [string]$ContainerUser = $(if ($env:CONTAINER_USER) { $env:CONTAINER_USER } else { 'dev' }),
    [string]$Proxy = $(($env:CLAUDE_PROXY, $env:HTTPS_PROXY, $env:HTTP_PROXY | Where-Object { $_ })[0]),
    [string]$NoProxy = $env:CLAUDE_NO_PROXY,
    [string]$Network = $(if ($env:CLAUDE_NET) { $env:CLAUDE_NET } else { 'claude-egress' }),
    [string]$RelayName = $(if ($env:CLAUDE_RELAY) { $env:CLAUDE_RELAY } else { 'claude-egress-relay' }),
    [switch]$NoTty
)

$ErrorActionPreference = 'Stop'

$containerHome = "/home/$ContainerUser"
$homeDir = if ($env:HOME) { $env:HOME } else { $env:USERPROFILE }
$relayPort = 3128

$usedNames = [System.Collections.Generic.List[string]]::new()
function New-MountName([string]$Path) {
    $base = Split-Path -Leaf $Path.TrimEnd('/', '\')
    if ([string]::IsNullOrEmpty($base)) { $base = 'root' }
    $name = $base
    $i = 2
    while ($usedNames.Contains($name)) {
        $name = "$base-$i"
        $i++
    }
    $usedNames.Add($name)
    return $name
}

# Splits a proxy URL into its credentials (may be empty), host and port.
function Split-ProxyUrl([string]$Url) {
    $scheme = ''
    $rest = $Url
    if ($Url -match '^([a-zA-Z][a-zA-Z0-9+.-]*)://(.*)$') {
        $scheme = $Matches[1].ToLower()
        $rest = $Matches[2]
    }
    $rest = ($rest -split '/', 2)[0]

    $creds = ''
    $hostPort = $rest
    $at = $rest.LastIndexOf('@')
    if ($at -ge 0) {
        $creds = $rest.Substring(0, $at)
        $hostPort = $rest.Substring($at + 1)
    }

    if ($hostPort -match '^(\[[^\]]+\])(?::(\d+))?$') {
        $proxyHost = $Matches[1]
        $port = $Matches[2]
    }
    elseif ($hostPort -match '^([^:]+)(?::(\d+))?$') {
        $proxyHost = $Matches[1]
        $port = $Matches[2]
    }
    else {
        throw "cannot parse proxy: $Url"
    }

    if (-not $port) {
        $port = switch ($scheme) {
            'https' { '443' }
            'http' { '80' }
            default { '8080' }
        }
        Write-Host "no proxy port given, assuming $port"
    }
    if ($scheme -eq 'https') {
        Write-Warning 'the relay forwards raw TCP, so a TLS-terminating proxy will fail certificate validation'
    }

    return [pscustomobject]@{ Creds = $creds; Host = $proxyHost; Port = $port }
}

# Creates the internal network if missing, and refuses a same-named one that
# would let traffic out.
function Initialize-EgressNetwork {
    $internal = & docker network inspect -f '{{.Internal}}' $Network 2>$null
    if ($LASTEXITCODE -eq 0) {
        if ($internal.Trim() -ne 'true') {
            throw "network $Network exists but is not internal; remove it or set CLAUDE_NET"
        }
        return
    }
    & docker network create --internal $Network | Out-Null
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "created internal network $Network"
}

# Starts (or recreates, when the upstream changed) the only container bridging
# the internal network to the outside world.
function Initialize-Relay($Upstream) {
    $want = "$($Upstream.Host):$($Upstream.Port)"
    $label = & docker inspect -f '{{index .Config.Labels "claude.upstream"}}' $RelayName 2>$null
    if ($LASTEXITCODE -eq 0 -and $label.Trim() -eq $want) {
        $running = & docker inspect -f '{{.State.Running}}' $RelayName
        if ($running.Trim() -ne 'true') { & docker start $RelayName | Out-Null }
        return
    }

    & docker rm -f $RelayName 2>$null | Out-Null

    $target = $Upstream.Host
    $extra = @()
    if ($target -in @('localhost', '127.0.0.1', '::1', '[::1]', 'host.docker.internal')) {
        $target = 'host.docker.internal'
        $extra += @('--add-host', 'host.docker.internal:host-gateway')
    }

    # Created, bridged, then started: socat must never come up on a container
    # that cannot yet resolve the upstream.
    $relayArgs = @(
        'create'
        '--name', $RelayName
        '--restart', 'unless-stopped'
        '--network', $Network
        '--network-alias', 'proxy'
        '--label', "claude.upstream=$want"
        '--cap-drop', 'ALL'
        '--security-opt', 'no-new-privileges'
    ) + $extra + @(
        "${Image}:${Tag}"
        'socat', "TCP-LISTEN:${relayPort},fork,reuseaddr", "TCP:${target}:$($Upstream.Port)"
    )
    & docker @relayArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & docker network connect bridge $RelayName
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & docker start $RelayName | Out-Null
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "relay $RelayName -> $want"
}

# Everything Claude Code persists lives in one directory mount; CLAUDE_CONFIG_DIR
# keeps .claude.json inside it instead of at $HOME, where an atomic rewrite would
# break a single-file bind mount.
$configDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $homeDir '.claude' }

if (-not (Test-Path -LiteralPath $configDir)) {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
}

$hostConfigFile = Join-Path $homeDir '.claude.json'
$containedConfigFile = Join-Path $configDir '.claude.json'
if ((-not (Test-Path -LiteralPath $containedConfigFile)) -and (Test-Path -LiteralPath $hostConfigFile)) {
    Copy-Item -LiteralPath $hostConfigFile -Destination $containedConfigFile
}

Initialize-EgressNetwork

$runArgs = @(
    'run'
    '--rm'
    '--network', $Network
    '--cap-drop', 'NET_ADMIN'
    '--cap-drop', 'NET_RAW'
    '-v', "${configDir}:${containerHome}/.claude"
    '-e', "CLAUDE_CONFIG_DIR=${containerHome}/.claude"
)

if ($Proxy) {
    $upstream = Split-ProxyUrl $Proxy
    Initialize-Relay $upstream
    $credPart = if ($upstream.Creds) { "$($upstream.Creds)@" } else { '' }
    $inProxy = "http://${credPart}proxy:${relayPort}"
    $noProxyVal = 'localhost,127.0.0.1,::1,proxy'
    if ($NoProxy) { $noProxyVal += ",$NoProxy" }
    foreach ($name in @('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')) {
        $runArgs += @('-e', "${name}=${inProxy}")
    }
    foreach ($name in @('NO_PROXY', 'no_proxy')) {
        $runArgs += @('-e', "${name}=${noProxyVal}")
    }
}
else {
    Write-Warning 'no proxy configured; the container will have no network access'
}

$pwdPath = (Get-Location).Path
$workdirName = New-MountName $pwdPath
$runArgs += @('-v', "${pwdPath}:/workspace/${workdirName}")
Write-Host "mount $pwdPath -> /workspace/$workdirName (workdir)"

foreach ($p in $Paths) {
    $resolved = Resolve-Path -LiteralPath $p -ErrorAction SilentlyContinue
    if (-not $resolved) {
        Write-Error "no such path: $p"
        exit 1
    }
    $abs = $resolved.Path
    $name = New-MountName $abs
    $runArgs += @('-v', "${abs}:/workspace/${name}")
    Write-Host "mount $abs -> /workspace/$name"
}

$runArgs += @('-w', "/workspace/${workdirName}")

if (-not $NoTty) { $runArgs += '-it' }

$gitConfig = Join-Path $homeDir '.gitconfig'
if (Test-Path -LiteralPath $gitConfig) {
    $runArgs += @('-v', "${gitConfig}:${containerHome}/.gitconfig:ro")
}

foreach ($var in @('ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN',
                   'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
                   'GH_TOKEN', 'GITHUB_TOKEN')) {
    if (Get-Item -LiteralPath "env:$var" -ErrorAction SilentlyContinue) {
        $runArgs += @('-e', $var)
    }
}

$runArgs += "${Image}:${Tag}"
$runArgs += 'claude'
if ($ClaudeArgs) { $runArgs += $ClaudeArgs }

& docker @runArgs
exit $LASTEXITCODE
