#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [string]$Image = $(if ($env:CLAUDE_IMAGE) { $env:CLAUDE_IMAGE } else { 'claude-dev' }),
    [string]$Tag = $(if ($env:CLAUDE_TAG) { $env:CLAUDE_TAG } else { 'latest' }),
    [string]$ContainerUser = $(if ($env:CONTAINER_USER) { $env:CONTAINER_USER } else { 'dev' }),
    [string]$NodeMajor = $(if ($env:NODE_MAJOR) { $env:NODE_MAJOR } else { '22' }),
    [string]$DotnetChannel = $(if ($env:DOTNET_CHANNEL) { $env:DOTNET_CHANNEL } else { '10.0' }),
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DockerArgs
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

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
$buildArgs += $scriptDir

& docker @buildArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "built ${Image}:${Tag}"
