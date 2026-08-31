#!/usr/bin/env pwsh
<#
.SYNOPSIS
Ensures the egress proxy is running, then starts claude in a container.

.DESCRIPTION
The current directory is always mounted and used as the working directory. Each
additional path given is mounted read-write under /workspace, named after the
last component of that path.

The claude container runs on an internal container network with no route off the
host. Its only way out is the tinyproxy container, which sits on that network as
http://proxy:3128 and on a second, outward-facing network named by -Bridge (or
CLAUDE_BRIDGE) and created when missing. That proxy goes out over the host
connection, or forwards to the corporate proxy named by -Proxy (or CLAUDE_PROXY /
HTTPS_PROXY / HTTP_PROXY), except for the hosts, domains (.corp.example) and
networks (10.0.0.0/8) named by -NoProxy (or CLAUDE_NO_PROXY / NO_PROXY), which it
reaches directly. A proxy container left over from an earlier start is reused,
and recreated when its upstream, that list or either network differs from the one
in the current environment.

The container frontend is docker or podman, chosen with -Engine or
CONTAINER_ENGINE.

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
    [ValidateSet('docker', 'podman')]
    [string]$Engine = $(if ($env:CONTAINER_ENGINE) { $env:CONTAINER_ENGINE } else { 'docker' }),
    [string]$Image = $(if ($env:CLAUDE_IMAGE) { $env:CLAUDE_IMAGE } else { 'claude-dev' }),
    [string]$Tag = $(if ($env:CLAUDE_TAG) { $env:CLAUDE_TAG } else { 'latest' }),
    [string]$ContainerUser = $(if ($env:CONTAINER_USER) { $env:CONTAINER_USER } else { 'dev' }),
    [string]$Proxy = $($env:CLAUDE_PROXY, $env:HTTPS_PROXY, $env:HTTP_PROXY | Where-Object { $_ } | Select-Object -First 1),
    [string]$NoProxy = $($env:CLAUDE_NO_PROXY, $env:NO_PROXY | Where-Object { $_ } | Select-Object -First 1),
    [string]$Network = $(if ($env:CLAUDE_NET) { $env:CLAUDE_NET } else { 'claude-egress' }),
    [string]$Bridge = $env:CLAUDE_BRIDGE,
    [string]$UserNs = $env:CLAUDE_USERNS,
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

$hostInternal = if ($Engine -eq 'podman') { 'host.containers.internal' } else { 'host.docker.internal' }
if (-not $Bridge) { $Bridge = "$Network-out" }

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
# whether that host is the container host itself.
function Resolve-Upstream {
    if (-not $Proxy) {
        return [pscustomobject]@{ Spec = ''; HostGateway = $false }
    }
    $parsed = Split-ProxyUrl $Proxy
    $target = $parsed.Host
    $hostGateway = $false
    if ($target -in @('localhost', '127.0.0.1', '::1', '[::1]', 'host.docker.internal', 'host.containers.internal')) {
        $target = $hostInternal
        $hostGateway = $true
    }
    $credPart = if ($parsed.Creds) { "$($parsed.Creds)@" } else { '' }
    return [pscustomobject]@{ Spec = "${credPart}${target}:$($parsed.Port)"; HostGateway = $hostGateway }
}

# Creates the internal network if missing, and refuses a same-named one that
# would let the claude container out on its own.
function Initialize-EgressNetwork {
    $internal = & $Engine network inspect -f '{{.Internal}}' $Network 2>$null
    if ($LASTEXITCODE -eq 0) {
        if ($internal.Trim() -ne 'true') {
            throw "network $Network exists but is not internal; remove it or set CLAUDE_NET"
        }
        return
    }
    & $Engine network create --internal $Network | Out-Null
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "created internal network $Network"
}

# Creates the outward-facing network if missing. The engines' predefined bridge
# networks serve no DNS of their own, and an internal network's resolver refuses
# to forward, so a proxy on those two alone resolves nothing at all: neither its
# upstream nor the sites it is asked to fetch.
function Initialize-BridgeNetwork {
    $internal = & $Engine network inspect -f '{{.Internal}}' $Bridge 2>$null
    if ($LASTEXITCODE -eq 0) {
        if ($internal.Trim() -eq 'true') {
            throw "network $Bridge is internal; the proxy cannot reach the outside through it"
        }
        if ($Engine -eq 'podman') {
            $dns = & $Engine network inspect -f '{{.DNSEnabled}}' $Bridge 2>$null
            if ($LASTEXITCODE -eq 0 -and $dns.Trim() -eq 'false') {
                Write-Warning "network $Bridge serves no DNS, the proxy will not resolve host names"
            }
        }
        return
    }
    & $Engine network create $Bridge | Out-Null
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "created outward network $Bridge"
}

# Blocks until tinyproxy answers on its own port. A container whose entrypoint
# fails still starts cleanly, and the restart policy then hides it in a crash
# loop, so nothing may rely on the proxy before it has served a request.
function Wait-Proxy {
    for ($i = 0; $i -lt 20; $i++) {
        & $Engine exec $ProxyName sh -c "http_proxy=http://127.0.0.1:${proxyListen} wget -q -O /dev/null http://tinyproxy.stats/" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Milliseconds 250
    }
    Write-Error "proxy $ProxyName is not answering on port $proxyListen; see $Engine logs $ProxyName"
    exit 1
}

# Starts the egress proxy, recreating it when its upstream, the hosts kept off
# that upstream or a network changed. tinyproxy reads both once at start, so a
# change only reaches it through a container created anew with it.
function Initialize-Proxy($Upstream) {
    $labels = & $Engine inspect -f '{{index .Config.Labels "claude.upstream"}}|{{index .Config.Labels "claude.noupstream"}}|{{index .Config.Labels "claude.network"}}|{{index .Config.Labels "claude.bridge"}}' $ProxyName 2>$null
    $current = if ($LASTEXITCODE -eq 0 -and $labels) { $labels.Trim() } else { '' }
    if ($current -eq "$($Upstream.Spec)|$NoProxy|$Network|$Bridge") {
        $running = & $Engine inspect -f '{{.State.Running}}' $ProxyName
        if ($running.Trim() -ne 'true') { & $Engine start $ProxyName | Out-Null }
        Wait-Proxy
        return
    }
    if ($current) { Write-Host "proxy settings changed, recreating $ProxyName" }

    & $Engine image inspect "${ProxyImage}:${ProxyTag}" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        & (Join-Path $scriptDir 'build.ps1') proxy -Engine $Engine
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    & $Engine rm -f $ProxyName 2>$null | Out-Null

    $createArgs = @(
        'create'
        '--name', $ProxyName
        '--restart', 'unless-stopped'
        '--network', $Network
        '--network-alias', 'proxy'
        '--label', "claude.upstream=$($Upstream.Spec)"
        '--label', "claude.noupstream=$NoProxy"
        '--label', "claude.network=$Network"
        '--label', "claude.bridge=$Bridge"
        '--cap-drop', 'ALL'
        '--security-opt', 'no-new-privileges'
    )
    if ($Upstream.Spec) { $createArgs += @('-e', "UPSTREAM_PROXY=$($Upstream.Spec)") }
    if ($NoProxy) { $createArgs += @('-e', "NO_UPSTREAM=$NoProxy") }
    if ($Upstream.HostGateway) { $createArgs += @('--add-host', "${hostInternal}:host-gateway") }
    if ($ProxyPublish) { $createArgs += @('-p', "${ProxyBind}:${ProxyPublish}:${proxyListen}") }
    $createArgs += "${ProxyImage}:${ProxyTag}"

    # Created, bridged, then started: tinyproxy must never come up on a container
    # that cannot yet resolve its upstream.
    & $Engine @createArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Engine network connect $Bridge $ProxyName
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Engine start $ProxyName | Out-Null
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Wait-Proxy

    $described = if ($Upstream.Spec) { $Upstream.Spec -replace '^.*@', '' } else { 'direct' }
    Write-Host "proxy $ProxyName serving $Network -> $described"
}

& $Engine image inspect "${Image}:${Tag}" 2>$null | Out-Null
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
Initialize-BridgeNetwork
Initialize-Proxy $upstream

$inProxy = "http://proxy:${proxyListen}"
$noProxyVal = 'localhost,127.0.0.1,::1,proxy'

# Rootless podman maps the invoking user to root inside, which would leave the
# bind-mounted workspace owned by the wrong uid for the container user.
if (-not $PSBoundParameters.ContainsKey('UserNs') -and $null -eq $env:CLAUDE_USERNS -and $Engine -eq 'podman') {
    $rootless = & $Engine info -f '{{.Host.Security.Rootless}}' 2>$null
    if ($LASTEXITCODE -eq 0 -and $rootless.Trim() -eq 'true') { $UserNs = 'keep-id' }
}

$runArgs = @(
    'run'
    '--rm'
    '--network', $Network
    '--cap-drop', 'NET_ADMIN'
    '--cap-drop', 'NET_RAW'
    '-v', "${configDir}:${containerHome}/.claude"
    '-e', "CLAUDE_CONFIG_DIR=${containerHome}/.claude"
)
if ($UserNs) { $runArgs += @('--userns', $UserNs) }

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

# The terminal type and COLORTERM decide what the programs inside are willing to
# emit. Windows consoles set neither, so name what Windows Terminal and modern
# conhost actually support; under pwsh on Linux the host values win.
if (-not $NoTty) {
    $runArgs += '-it'
    $termVal = if ($env:TERM) { $env:TERM } else { 'xterm-256color' }
    $colorVal = if ($env:COLORTERM) { $env:COLORTERM } else { 'truecolor' }
    $runArgs += @('-e', "TERM=$termVal", '-e', "COLORTERM=$colorVal")
    foreach ($name in @('TERM_PROGRAM', 'TERM_PROGRAM_VERSION')) {
        if (Get-Item -LiteralPath "env:$name" -ErrorAction SilentlyContinue) {
            $runArgs += @('-e', $name)
        }
    }
}

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

& $Engine @runArgs
exit $LASTEXITCODE
