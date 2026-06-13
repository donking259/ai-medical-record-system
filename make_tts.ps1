Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoice('Microsoft Huihui Desktop')
$synth.Rate = -1
$synth.SetOutputToWaveFile('data\uploads\test-cn-tts.wav')
$text = [string]::Concat(
  [char]0x533B,[char]0x751F,[char]0x95EE,[char]0xFF0C,
  [char]0x5934,[char]0x6655,[char]0x5927,[char]0x6982,[char]0x6709,[char]0x591A,[char]0x4E45,[char]0x4E86,[char]0x3002,
  [char]0x60A3,[char]0x8005,[char]0x8BF4,[char]0xFF0C,
  [char]0x597D,[char]0x51E0,[char]0x4E2A,[char]0x6708,[char]0x4E86,[char]0xFF0C,
  [char]0x662F,[char]0x4E00,[char]0x9635,[char]0x4E00,[char]0x9635,[char]0x7684,[char]0x5934,[char]0x6655,[char]0x3002
)
$synth.Speak($text)
$synth.Dispose()
