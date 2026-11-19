# Puente local a Windows.Media.Ocr. Sin red, claves ni cambios de configuracion.
param([switch]$Estado, [string]$Imagen)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
    $idiomas = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages)
    $idioma = $idiomas | Where-Object LanguageTag -eq 'es-MX' | Select-Object -First 1
    if ($null -eq $idioma) {
        $idioma = $idiomas | Where-Object { $_.LanguageTag -like 'es-*' } | Select-Object -First 1
    }
    if ($null -eq $idioma) {
        throw 'Windows no tiene instalado un idioma OCR en espanol.'
    }
    $maximo = [int][Windows.Media.Ocr.OcrEngine]::MaxImageDimension
    if ($Estado) {
        @{ disponible = $true; idioma = $idioma.LanguageTag;
           idiomas = @($idiomas | ForEach-Object LanguageTag); max_dimension = $maximo } |
            ConvertTo-Json -Compress
        exit 0
    }
    if (-not $Imagen -or -not [System.IO.File]::Exists($Imagen)) {
        throw 'No existe la imagen temporal para OCR.'
    }

    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
    $null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime]
    $script:asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
        $_.GetParameters().Count -eq 1 -and $_.GetGenericArguments().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    function Esperar($Operacion, [Type]$Tipo) {
        $tarea = $script:asTask.MakeGenericMethod($Tipo).Invoke($null, @($Operacion))
        return $tarea.GetAwaiter().GetResult()
    }

    $archivo = Esperar ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Imagen)) ([Windows.Storage.StorageFile])
    $flujo = Esperar ($archivo.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Esperar ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($flujo)) ([Windows.Graphics.Imaging.BitmapDecoder])
        if ($decoder.PixelWidth -gt $maximo -or $decoder.PixelHeight -gt $maximo) {
            throw 'La imagen excede el limite del motor OCR de Windows.'
        }
        $bitmap = Esperar ($decoder.GetSoftwareBitmapAsync(
            [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
            [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)) ([Windows.Graphics.Imaging.SoftwareBitmap])
        try {
            $motor = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($idioma)
            if ($null -eq $motor) { throw 'No se pudo iniciar el motor OCR de Windows.' }
            $resultado = Esperar ($motor.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $lineas = @($resultado.Lines | ForEach-Object {
                @{ texto = $_.Text; palabras = @($_.Words | ForEach-Object {
                    @{ texto = $_.Text; rect = @($_.BoundingRect.X, $_.BoundingRect.Y,
                                              $_.BoundingRect.Width, $_.BoundingRect.Height) }
                }) }
            })
            @{ texto = (($lineas | ForEach-Object { $_.texto }) -join "`n"); lineas = $lineas;
               idioma = $idioma.LanguageTag; ancho_px = $bitmap.PixelWidth; alto_px = $bitmap.PixelHeight } |
                ConvertTo-Json -Depth 10 -Compress
        } finally { $bitmap.Dispose() }
    } finally { $flujo.Dispose() }
} catch {
    @{ error = $_.Exception.Message } | ConvertTo-Json -Compress
    exit 1
}
