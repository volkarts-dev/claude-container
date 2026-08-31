#!/usr/bin/env pwsh
<#
.SYNOPSIS
Ensures the egress proxy is running, then starts claude in a container.

.DESCRIPTION
The current directory is always mounted and used as the working directory. Each
additional path given is mounted read-write under /workspace, named after the
last component of that path.

The claude container runs on an internal Docker network with no route off the
host. Its only way out is the tinyproxy container, which sits on that network as
http://proxy:3128 and on the default bridge. That proxy goes out over the host
connection, or forwards to the corporate proxy named by -Proxy (or CLAUDE_PROXY
/ HTTPS_PROXY / HTTP_PROXY).

The proxy image is built on demand; the dev image has to be built beforehand
with ./build.ps1.

.EXAMPLE
./start.ps1 ..\other-repo C:\data -ClaudeArgs --resume
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
    [string]$ProxyImage = $(if ($env:PROXY_IMAGE) { $env:PROXY_IMAGE } else { 'claude-proxy' }),
    [string]$ProxyTag = $(if ($env:PROXY_TAG) { $env:PROXY_TAG } else { 'latest' }),
    [string]$ProxyName = $(if ($env:PROXY_CONTAINER) { $env:PROXY_CONTAINER } else { 'claude-proxy' }),
    [string]$ProxyPublish = $env:PROXY_PORT,
    [string]$ProxyBind = $(if ($env:PROXY_BIND) { $env:PROXY_BIND } else { '127.0.0.1' }),
    [switch]$NoTty
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$containerHome = "/home/$ContainerUser"
$homeDir = if ($env:HOME) { $env:HOME } else { $env:USERPROFILE }
$proxyListen = 3128

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
        Write-Warning 'tinyproxy talks to the upstream in cleartext, so an https:// proxy will not work'
    }

    return [pscustomobject]@{ Creds = $creds; Host = $proxyHost; Port = $port }
}

# The [creds@]host:port tinyproxy forwards to, empty for direct egress, plus
# whether that host is the docker host itself.
function Resolve-Upstream {
    if (-not $Proxy) {
        return [pscustomobject]@{ Spec = ''; HostGateway = $false }
    }
    $parsed = Split-ProxyUrl $Proxy
    $target = $parsed.Host
    $hostGateway = $false
    if ($target -in @('localhost', '127.0.0.1', '::1', '[::1]', 'host.docker.internal')) {
        $target = 'host.docker.internal'
        $hostGateway = $true
    }
    $credPart = if ($parsed.Creds) { "$($parsed.Creds)@" } else { '' }
    return [pscustomobject]@{ Spec = "${credPart}${target}:$($parsed.Port)"; HostGateway = $hostGateway }
}

# Creates the internal network if missing, and refuses a same-named one that
# would let the claude container out on its own.
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

# Starts the egress proxy, recreating it when its upstream or network changed.
function Initialize-Proxy($Upstream) {
    $labels = & docker inspect -f '{{index .Config.Labels "claude.upstream"}}|{{index .Config.Labels "claude.network"}}' $ProxyName 2>$null
    if ($LASTEXITCODE -eq 0 -and $labels.Trim() -eq "$($Upstream.Spec)|$Network") {
        $running = & docker inspect -f '{{.State.Running}}' $ProxyName
        if ($running.Trim() -ne 'true') { & docker start $ProxyName | Out-Null }
        return
    }

    & docker image inspect "${ProxyImage}:${ProxyTag}" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        & (Join-Path $scriptDir 'build.ps1') proxy
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    & docker rm -f $ProxyName 2>$null | Out-Null

    $createArgs = @(
        'create'
        '--name', $ProxyName
        '--restart', 'unless-stopped'
        '--network', $Network
        '--network-alias', 'proxy'
        '--label', "claude.upstream=$($Upstream.Spec)"
        '--label', "claude.network=$Network"
        '--cap-drop', 'ALL'
        '--security-opt', 'no-new-privileges'
    )
    if ($Upstream.Spec) { $createArgs += @('-e', "UPSTREAM_PROXY=$($Upstream.Spec)") }
    if ($Upstream.HostGateway) { $createArgs += @('--add-host', 'host.docker.internal:host-gateway') }
    if ($ProxyPublish) { $createArgs += @('-p', "${ProxyBind}:${ProxyPublish}:${proxyListen}") }
    $createArgs += "${ProxyImage}:${ProxyTag}"

    # Created, bridged, then started: tinyproxy must never come up on a container
    # that cannot yet resolve its upstream.
    & docker @createArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & docker network connect bridge $ProxyName
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & docker start $ProxyName | Out-Null
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $described = if ($Upstream.Spec) { $Upstream.Spec -replace '^.*@', '' } else { 'direct' }
    Write-Host "proxy $ProxyName serving $Network -> $described"
}

& docker image inspect "${Image}:${Tag}" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "image ${Image}:${Tag} is missing; run $scriptDir/build.ps1"
    exit 1
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

$upstream = Resolve-Upstream
Initialize-EgressNetwork
Initialize-Proxy $upstream

$inProxy = "http://proxy:${proxyListen}"
$noProxyVal = 'localhost,127.0.0.1,::1,proxy'
if ($NoProxy) { $noProxyVal += ",$NoProxy" }

$runArgs = @(
    'run'
    '--rm'
    '--network', $Network
    '--cap-drop', 'NET_ADMIN'
    '--cap-drop', 'NET_RAW'
    '-v', "${configDir}:${containerHome}/.claude"
    '-e', "CLAUDE_CONFIG_DIR=${containerHome}/.claude"
)

foreach ($name in @('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')) {
    $runArgs += @('-e', "${name}=${inProxy}")
}
foreach ($name in @('NO_PROXY', 'no_proxy')) {
    $runArgs += @('-e', "${name}=${noProxyVal}")
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
