param(
    [Parameter(Position = 0)]
    [string]$Command = "init",

    [Parameter(Position = 1)]
    [string]$Target = ".",

    [Parameter(Position = 2, ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

if ($Command -notin @("init", "impact")) {
    Write-Error "Unsupported command: $Command"
    exit 1
}

$issue = ""
if ($ExtraArgs) {
    $issue = ($ExtraArgs -join " ")
}

if ([string]::IsNullOrWhiteSpace($issue)) {
    & py -3 "$PSScriptRoot\tools\build_code_review_graph.py" $Command $Target
}
else {
    & py -3 "$PSScriptRoot\tools\build_code_review_graph.py" $Command $Target $issue
}
exit $LASTEXITCODE
