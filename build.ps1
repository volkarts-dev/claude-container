#!/usr/bin/env pwsh

# Without PositionalBinding off, -Engine and the parameters after it are
# positional too and swallow arguments meant for -DockerArgs.
[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Position = 0)]
    [ValidateSet('claude', 'proxy', 'all')]
    [string]$Target = 'all',
    [ValidateSet('docker', 'podman')]
    [string]$Engine = $(if ($env:CONTAINER_ENGINE) { $env:CONTAINER_ENGINE } else { 'docker' }),
    [string]$Image = $(if ($env:CLAUDE_IMAGE) { $env:CLAUDE_IMAGE } else { 'claude-dev' }),
    [string]$Tag = $(if ($env:CLAUDE_TAG) { $env:CLAUDE_TAG } else { 'latest' }),
    [string]$ContainerUser = $(if ($env:CONTAINER_USER) { $env:CONTAINER_USER } else { 'dev' }),
    [string]$NodeMajor = $(if ($env:NODE_MAJOR) { $env:NODE_MAJOR } else { '22' }),
    [string]$DotnetChannel = $(if ($env:DOTNET_CHANNEL) { $env:DOTNET_CHANNEL } else { '10.0' }),
    [string]$ProxyImage = $(if ($env:PROXY_IMAGE) { $env:PROXY_IMAGE } else { 'claude-proxy' }),
    [string]$ProxyTag = $(if ($env:PROXY_TAG) { $env:PROXY_TAG } else { 'latest' }),
    [switch]$Update,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DockerArgs
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$updateArgs = if ($Update) { @('--pull', '--no-cache') } else { @() }

function Build-ClaudeImage {
    if ($IsWindows -or $null -eq $IsWindows) {
        $userUid = '1000'
        $userGid = '1000'
    }
    else {
        $userUid = (& id -u).Trim()
        $userGid = (& id -g).Trim()
    }

    $buildArgs = @(
        'build'
        '--build-arg', "USER_UID=$userUid"
        '--build-arg', "USER_GID=$userGid"
        '--build-arg', "USERNAME=$ContainerUser"
        '--build-arg', "NODE_MAJOR=$NodeMajor"
        '--build-arg', "DOTNET_CHANNEL=$DotnetChannel"
        '-t', "${Image}:${Tag}"
    )
    $buildArgs += $updateArgs
    if ($DockerArgs) { $buildArgs += $DockerArgs }
    $buildArgs += (Join-Path $scriptDir 'claude')

    & $Engine @buildArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "built ${Image}:${Tag}"
}

function Build-ProxyImage {
    $buildArgs = @('build', '-t', "${ProxyImage}:${ProxyTag}")
    $buildArgs += $updateArgs
    if ($DockerArgs) { $buildArgs += $DockerArgs }
    $buildArgs += (Join-Path $scriptDir 'proxy')

    & $Engine @buildArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "built ${ProxyImage}:${ProxyTag}"
}

if ($Target -in @('all', 'claude')) { Build-ClaudeImage }
if ($Target -in @('all', 'proxy')) { Build-ProxyImage }
