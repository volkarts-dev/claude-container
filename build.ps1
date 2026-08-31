#!/usr/bin/env pwsh
<#
.SYNOPSIS
Builds the container images.

.DESCRIPTION
Target is claude or proxy; without one both are built.

.EXAMPLE
./build.ps1

.EXAMPLE
./build.ps1 proxy -DockerArgs --no-cache
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('claude', 'proxy', 'all')]
    [string]$Target = 'all',
    [string]$Image = $(if ($env:CLAUDE_IMAGE) { $env:CLAUDE_IMAGE } else { 'claude-dev' }),
    [string]$Tag = $(if ($env:CLAUDE_TAG) { $env:CLAUDE_TAG } else { 'latest' }),
    [string]$ContainerUser = $(if ($env:CONTAINER_USER) { $env:CONTAINER_USER } else { 'dev' }),
    [string]$NodeMajor = $(if ($env:NODE_MAJOR) { $env:NODE_MAJOR } else { '22' }),
    [string]$DotnetChannel = $(if ($env:DOTNET_CHANNEL) { $env:DOTNET_CHANNEL } else { '10.0' }),
    [string]$ProxyImage = $(if ($env:PROXY_IMAGE) { $env:PROXY_IMAGE } else { 'claude-proxy' }),
    [string]$ProxyTag = $(if ($env:PROXY_TAG) { $env:PROXY_TAG } else { 'latest' }),
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DockerArgs
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

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
    if ($DockerArgs) { $buildArgs += $DockerArgs }
    $buildArgs += (Join-Path $scriptDir 'claude')

    & docker @buildArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "built ${Image}:${Tag}"
}

function Build-ProxyImage {
    $buildArgs = @('build', '-t', "${ProxyImage}:${ProxyTag}")
    if ($DockerArgs) { $buildArgs += $DockerArgs }
    $buildArgs += (Join-Path $scriptDir 'proxy')

    & docker @buildArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "built ${ProxyImage}:${ProxyTag}"
}

if ($Target -in @('all', 'claude')) { Build-ClaudeImage }
if ($Target -in @('all', 'proxy')) { Build-ProxyImage }
