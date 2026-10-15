param(
    [Parameter(Mandatory=$true)][string]$InputDocument,
    [Parameter(Mandatory=$true)][string]$OutputPdf,
    [switch]$Update
)
$ErrorActionPreference = 'Stop'
$inputPath = (Resolve-Path -LiteralPath $InputDocument).Path
$pdfPath = [System.IO.Path]::GetFullPath($OutputPdf)
$word = $null
$document = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $document = $word.Documents.Open($inputPath, $false, (-not $Update.IsPresent))
    if ($Update) {
        $document.Fields.Update() | Out-Null
        $document.Repaginate()
        foreach ($toc in $document.TablesOfContents) {
            $toc.Update()
            $toc.Range.Font.Name = 'Times New Roman'
            $toc.Range.Font.Size = 14
            $toc.Range.Font.Bold = 0
            $toc.Range.ParagraphFormat.Alignment = 0
            $toc.Range.ParagraphFormat.LeftIndent = 0
            $toc.Range.ParagraphFormat.FirstLineIndent = 0
            $toc.Range.ParagraphFormat.RightIndent = 0
            $toc.Range.ParagraphFormat.SpaceBefore = 0
            $toc.Range.ParagraphFormat.SpaceAfter = 0
            $toc.Range.ParagraphFormat.LineSpacingRule = 1
        }
        $document.Repaginate()
        foreach ($toc in $document.TablesOfContents) { $toc.UpdatePageNumbers() }
        $document.Save()
    }
    $document.ExportAsFixedFormat($pdfPath, 17)
    Write-Output ("Rendered {0} pages: {1}" -f $document.ComputeStatistics(2), $pdfPath)
} finally {
    if ($null -ne $document) { $document.Close(0) }
    if ($null -ne $word) { $word.Quit() }
}
