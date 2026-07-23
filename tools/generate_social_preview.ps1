param(
    [string]$OutputPath
)

Add-Type -AssemblyName System.Drawing

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputPath) {
    $OutputPath = Join-Path $repoRoot 'docs\images\snapmaker-u1-print-from-phone-telegram-flow.png'
}

$width = 1280
$height = 640
$bitmap = [System.Drawing.Bitmap]::new($width, $height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit

$backgroundRect = [System.Drawing.Rectangle]::new(0, 0, $width, $height)
$background = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
    $backgroundRect,
    [System.Drawing.ColorTranslator]::FromHtml('#07121B'),
    [System.Drawing.ColorTranslator]::FromHtml('#12232D'),
    18
)
$graphics.FillRectangle($background, $backgroundRect)

$gridPen = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(20, 80, 201, 224), 1)
for ($x = 0; $x -lt $width; $x += 64) {
    $graphics.DrawLine($gridPen, $x, 0, $x, $height)
}
for ($y = 0; $y -lt $height; $y += 64) {
    $graphics.DrawLine($gridPen, 0, $y, $width, $y)
}

$white = [System.Drawing.ColorTranslator]::FromHtml('#F5F9FB')
$muted = [System.Drawing.ColorTranslator]::FromHtml('#AFC2CC')
$cyan = [System.Drawing.ColorTranslator]::FromHtml('#4CC9E8')
$orange = [System.Drawing.ColorTranslator]::FromHtml('#FF8A00')
$cardFill = [System.Drawing.Color]::FromArgb(232, 11, 29, 39)
$cardBorder = [System.Drawing.Color]::FromArgb(210, 76, 201, 232)

$fontFamily = 'Segoe UI'
$eyebrowFont = [System.Drawing.Font]::new($fontFamily, 16, [System.Drawing.FontStyle]::Bold)
$titleFont = [System.Drawing.Font]::new($fontFamily, 55, [System.Drawing.FontStyle]::Bold)
$subtitleFont = [System.Drawing.Font]::new($fontFamily, 22, [System.Drawing.FontStyle]::Regular)
$stepFont = [System.Drawing.Font]::new($fontFamily, 13, [System.Drawing.FontStyle]::Bold)
$cardTitleFont = [System.Drawing.Font]::new($fontFamily, 19, [System.Drawing.FontStyle]::Bold)
$cardDetailFont = [System.Drawing.Font]::new($fontFamily, 15, [System.Drawing.FontStyle]::Regular)
$badgeFont = [System.Drawing.Font]::new($fontFamily, 14, [System.Drawing.FontStyle]::Bold)

$whiteBrush = [System.Drawing.SolidBrush]::new($white)
$mutedBrush = [System.Drawing.SolidBrush]::new($muted)
$cyanBrush = [System.Drawing.SolidBrush]::new($cyan)
$orangeBrush = [System.Drawing.SolidBrush]::new($orange)
$cardBrush = [System.Drawing.SolidBrush]::new($cardFill)

$graphics.DrawString('OPEN-SOURCE SNAPMAKER U1 TOOLKIT', $eyebrowFont, $orangeBrush, 54, 42)
$graphics.DrawString('PRINT TO YOUR SNAPMAKER U1', $titleFont, $whiteBrush, 48, 88)
$graphics.DrawString('FROM YOUR PHONE', $titleFont, $orangeBrush, 48, 153)
$graphics.DrawString('Telegram  >  OrcaSlicer  >  Preview  >  Camera check  >  Human-approved print', $subtitleFont, $mutedBrush, 54, 238)

$badgeRect = [System.Drawing.RectangleF]::new(870, 44, 350, 42)
$badgePath = [System.Drawing.Drawing2D.GraphicsPath]::new()
$badgeRadius = 20
$badgePath.AddArc($badgeRect.X, $badgeRect.Y, $badgeRadius * 2, $badgeRadius * 2, 180, 90)
$badgePath.AddArc($badgeRect.Right - ($badgeRadius * 2), $badgeRect.Y, $badgeRadius * 2, $badgeRadius * 2, 270, 90)
$badgePath.AddArc($badgeRect.Right - ($badgeRadius * 2), $badgeRect.Bottom - ($badgeRadius * 2), $badgeRadius * 2, $badgeRadius * 2, 0, 90)
$badgePath.AddArc($badgeRect.X, $badgeRect.Bottom - ($badgeRadius * 2), $badgeRadius * 2, $badgeRadius * 2, 90, 90)
$badgePath.CloseFigure()
$badgeFill = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(45, 255, 138, 0))
$badgePen = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(160, 255, 138, 0), 1.5)
$graphics.FillPath($badgeFill, $badgePath)
$graphics.DrawPath($badgePen, $badgePath)
$graphics.DrawString('LOCAL  |  PRIVATE  |  APPROVAL-GATED', $badgeFont, $whiteBrush, 891, 55)

$steps = @(
    @{ Step = 'SEND'; Title = 'Telegram'; Detail = 'From your phone' },
    @{ Step = 'SLICE'; Title = 'OrcaSlicer'; Detail = 'Headless and local' },
    @{ Step = 'REVIEW'; Title = 'Plate Preview'; Detail = 'Toolpath + settings' },
    @{ Step = 'VERIFY'; Title = 'Camera Check'; Detail = 'Tool, filament, bed' },
    @{ Step = 'PRINT'; Title = 'Approve + Print'; Detail = 'Single-use gate' }
)

$cardY = 350
$cardWidth = 220
$cardHeight = 190
$gap = 20
$startX = 50
$cardPen = [System.Drawing.Pen]::new($cardBorder, 1.5)
$finalCardPen = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(230, 255, 138, 0), 2)
$arrowPen = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(180, 76, 201, 232), 2)
$arrowPen.CustomEndCap = [System.Drawing.Drawing2D.AdjustableArrowCap]::new(5, 5)

for ($i = 0; $i -lt $steps.Count; $i++) {
    $x = $startX + (($cardWidth + $gap) * $i)
    $rect = [System.Drawing.RectangleF]::new($x, $cardY, $cardWidth, $cardHeight)
    $path = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $r = 18
    $path.AddArc($rect.X, $rect.Y, $r * 2, $r * 2, 180, 90)
    $path.AddArc($rect.Right - ($r * 2), $rect.Y, $r * 2, $r * 2, 270, 90)
    $path.AddArc($rect.Right - ($r * 2), $rect.Bottom - ($r * 2), $r * 2, $r * 2, 0, 90)
    $path.AddArc($rect.X, $rect.Bottom - ($r * 2), $r * 2, $r * 2, 90, 90)
    $path.CloseFigure()

    $graphics.FillPath($cardBrush, $path)
    $graphics.DrawPath($(if ($i -eq 4) { $finalCardPen } else { $cardPen }), $path)

    $numberBrush = if ($i -eq 4) { $orangeBrush } else { $cyanBrush }
    $graphics.FillEllipse($numberBrush, $x + 20, $cardY + 22, 40, 40)
    $numberText = [string]($i + 1)
    $numberSize = $graphics.MeasureString($numberText, $stepFont)
    $graphics.DrawString($numberText, $stepFont, [System.Drawing.Brushes]::Black, $x + 40 - ($numberSize.Width / 2), $cardY + 32)

    $graphics.DrawString($steps[$i].Step, $stepFont, $numberBrush, $x + 72, $cardY + 32)
    $graphics.DrawString($steps[$i].Title, $cardTitleFont, $whiteBrush, $x + 20, $cardY + 86)
    $graphics.DrawString($steps[$i].Detail, $cardDetailFont, $mutedBrush, $x + 20, $cardY + 130)

    if ($i -lt ($steps.Count - 1)) {
        $arrowY = $cardY + ($cardHeight / 2)
        $graphics.DrawLine($arrowPen, $x + $cardWidth + 3, $arrowY, $x + $cardWidth + $gap - 4, $arrowY)
    }

    $path.Dispose()
}

$footerFont = [System.Drawing.Font]::new($fontFamily, 14, [System.Drawing.FontStyle]::Regular)
$graphics.DrawString('github.com/bbolinger/snapmaker-u1-toolkit', $footerFont, $mutedBrush, 50, 585)
$graphics.DrawString('Moonraker | Klipper | Local LLM optional', $footerFont, $mutedBrush, 910, 585)

$outputDirectory = Split-Path -Parent $OutputPath
[System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
$bitmap.Save($OutputPath, [System.Drawing.Imaging.ImageFormat]::Png)

$footerFont.Dispose()
$arrowPen.Dispose()
$finalCardPen.Dispose()
$cardPen.Dispose()
$badgePen.Dispose()
$badgeFill.Dispose()
$badgePath.Dispose()
$cardBrush.Dispose()
$orangeBrush.Dispose()
$cyanBrush.Dispose()
$mutedBrush.Dispose()
$whiteBrush.Dispose()
$badgeFont.Dispose()
$cardDetailFont.Dispose()
$cardTitleFont.Dispose()
$stepFont.Dispose()
$subtitleFont.Dispose()
$titleFont.Dispose()
$eyebrowFont.Dispose()
$gridPen.Dispose()
$background.Dispose()
$graphics.Dispose()
$bitmap.Dispose()

Write-Output $OutputPath
