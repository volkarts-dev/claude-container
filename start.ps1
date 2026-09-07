#!/usr/bin/env pwsh
$ErrorActionPreference = 'Stop'
$python = if (Get-Command python3 -ErrorAction SilentlyContinue) { 'python3' } else { 'python' }
& $python (Join-Path $PSScriptRoot 'start.py') @args
exit $LASTEXITCODE
