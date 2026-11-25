# Put the LINE Messaging API channel access token where CiRA's "LINE notify" node reads it
# (/home/cira/kitcheck_model/line.env on the GPU VM). The token is typed here, hidden, and never shown.
#   powershell -ExecutionPolicy Bypass -File cira\set_line_token.ps1
$sec = Read-Host -AsSecureString "LINE channel access token"
$tok = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
if ($tok -notmatch '^[A-Za-z0-9+/=._-]{20,}$') { Write-Host "That does not look like a channel access token."; exit 1 }
$g = "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
$f = @("--zone=us-central1-c", "--project=cira-core-515712", "--tunnel-through-iap", "--quiet", "--strict-host-key-checking=no")
$file = "/var/lib/docker/volumes/ciracore-home/_data/kitcheck_model/line.env"
& $g compute ssh kitcheck-gpu @f --command "echo LINE_TOKEN=$tok | sudo tee $file >/dev/null && sudo chown 1000:1000 $file && sudo chmod 600 $file && echo saved"
$tok = $null
