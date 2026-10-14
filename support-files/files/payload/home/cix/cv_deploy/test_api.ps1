param(
    [string]$BaseUrl = "http://192.168.133.1:17710",
    [string]$Model = "yolov11_n",
    [string]$Image = "$PSScriptRoot\test.jpg",
    [double]$Confidence = 0.25
)

$health = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 10
if (-not $health.ok) {
    throw "CV service is not healthy"
}

$uri = "$BaseUrl/v1/cv/infer/$Model`?confidence=$Confidence"
Invoke-RestMethod -Uri $uri -Method Post -ContentType "image/jpeg" -InFile $Image -TimeoutSec 180 |
    ConvertTo-Json -Depth 20
