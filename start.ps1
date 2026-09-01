#!/usr/bin/env pwsh

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
    [string]$Network = $(if ($env:CLAUDE_NET) { $env:CLAUDE_NET } else { 'claude-internal' }),
    [string]$Bridge = $env:CLAUDE_BRIDGE,
    [string]$UserNs = $env:CLAUDE_USERNS,
    [string]$ProxyImage = $(if ($env:PROXY_IMAGE) { $env:PROXY_IMAGE } else { 'claude-proxy' }),
    [string]$ProxyTag = $(if ($env:PROXY_TAG) { $env:PROXY_TAG } else { 'latest' }),
    [string]$ProxyName = $(if ($env:PROXY_CONTAINER) { $env:PROXY_CONTAINER } else { 'claude-proxy' }),
    [string]$ProxyPublish = $env:PROXY_PORT,
    [string]$ProxyBind = $(if ($env:PROXY_BIND) { $env:PROXY_BIND } else { '127.0.0.1' }),
    [switch]$NoTty,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

function Show-Usage {
    Write-Host @'
usage: start.ps1 [PATH...] [-ClaudeArgs CLAUDE_ARG...]

Ensures the egress proxy is running, then starts claude in a container. The
current directory is always mounted and used as the working directory. Each
additional PATH is mounted read-write under /workspace, named after the last
component of that path.

Anything given to -ClaudeArgs is passed on to claude itself.

Networking
  The claude container runs on an internal container network with no route off
  the host. Its only way out is the tinyproxy container, which sits on that
  network as http://proxy:3128 and on a second, outward-facing network. That
  proxy goes out over the host connection, or forwards to the corporate proxy
  named by -Proxy, except for the hosts named by -NoProxy. A proxy container
  left over from an earlier start is reused, and recreated when its upstream,
  that list or either network differs from the one in the current environment.

  The proxy image is built on demand; the dev image has to be built beforehand
  with ./build.ps1.

Parameters
  -Engine          container frontend, docker or podman (default docker,
                   env CONTAINER_ENGINE)
  -Proxy           corporate proxy the egress forwards to, e.g.
                   http://proxy.corp:3128 or http://user:pass@host:port; falls
                   back to CLAUDE_PROXY/HTTPS_PROXY/HTTP_PROXY, unset means
                   direct
  -NoProxy         comma-separated hosts, domains (.corp.example) and networks
                   (10.0.0.0/8) the proxy reaches directly instead of through
                   the upstream; falls back to CLAUDE_NO_PROXY/NO_PROXY
  -Network         internal network name (default claude-internal,
                   env CLAUDE_NET)
  -Bridge          outward-facing network the proxy reaches the outside on,
                   created when missing (default claude-egress,
                   env CLAUDE_BRIDGE); it has to carry DNS, which the engines'
                   predefined bridge networks do not
  -UserNs          --userns for the claude container; unset picks keep-id under
                   rootless podman and nothing otherwise (env CLAUDE_USERNS)
  -Image           dev image name (default claude-dev, env CLAUDE_IMAGE)
  -Tag             dev image tag (default latest, env CLAUDE_TAG)
  -ContainerUser   user inside the dev image (default dev, env CONTAINER_USER)
  -ProxyImage      proxy image name (default claude-proxy, env PROXY_IMAGE)
  -ProxyTag        proxy image tag (default latest, env PROXY_TAG)
  -ProxyName       proxy container name (default claude-proxy,
                   env PROXY_CONTAINER)
  -ProxyPublish    host port to publish the proxy on; unset publishes nothing
                   (env PROXY_PORT)
  -ProxyBind       host address for that port (default 127.0.0.1,
                   env PROXY_BIND)
  -NoTty           do not allocate a terminal for the claude container
'@
}

if ($Help) {
    Show-Usage
    exit 0
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$containerHome = "/home/$ContainerUser"
$homeDir = if ($env:HOME) { $env:HOME } else { $env:USERPROFILE }
$proxyListen = 3128

$hostInternal = if ($Engine -eq 'podman') { 'host.containers.internal' } else { 'host.docker.internal' }
if (-not $Bridge) { $Bridge = "claude-egress" }

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

function Initialize-InternalNetwork {
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

function Wait-Proxy {
    for ($i = 0; $i -lt 20; $i++) {
        & $Engine exec $ProxyName sh -c "http_proxy=http://127.0.0.1:${proxyListen} wget -q -O /dev/null http://tinyproxy.stats/" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Milliseconds 250
    }
    Write-Error "proxy $ProxyName is not answering on port $proxyListen; see $Engine logs $ProxyName"
    exit 1
}

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
Initialize-InternalNetwork
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
    '--security-opt', 'no-new-privileges'
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
                   'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX')) {
    if (Get-Item -LiteralPath "env:$var" -ErrorAction SilentlyContinue) {
        $runArgs += @('-e', $var)
    }
}

$runArgs += "${Image}:${Tag}"
$runArgs += 'claude'
if ($ClaudeArgs) { $runArgs += $ClaudeArgs }

& $Engine @runArgs
exit $LASTEXITCODE
