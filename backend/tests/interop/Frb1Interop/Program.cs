// Arnés de verificación de interoperabilidad entre License Manager (Python)
// y Frubber POS (C#). Imprime VERIFIED o REJECTED y sale con código 0 o 1.
//
// Uso:
//   Frb1Interop <licencia FRB1> <keyId> <llave publica SPKI base64>
//
// La llave publica se pasa como SubjectPublicKeyInfo en Base64, el mismo
// formato que embebe el POS en LicensePublicKeys.cs.

using System.Security.Cryptography;
using FrubberPOS.Application.Licensing;
using FrubberPOS.Domain.Licensing;
using FrubberPOS.Infrastructure.Licensing;

if (args.Length != 3)
{
    Console.Error.WriteLine("Uso: Frb1Interop <licencia> <keyId> <spkiBase64>");
    return 2;
}

var licenseText = args[0];
var expectedKeyId = args[1];
var publicKeySpkiBase64 = args[2];

if (!LicenseKeyFormat.TryParse(licenseText, out var parsed) || parsed is null)
{
    Console.WriteLine("REJECTED:formato");
    return 1;
}

if (!string.Equals(parsed.KeyId, expectedKeyId, StringComparison.Ordinal))
{
    Console.WriteLine("REJECTED:keyId");
    return 1;
}

ILicenseSignatureVerifier verifier;
try
{
    var spki = Convert.FromBase64String(publicKeySpkiBase64);
    using var ecdsa = ECDsa.Create();
    ecdsa.ImportSubjectPublicKeyInfo(spki, out _);

    // Verificamos con la MISMA rutina que usa el POS en producción.
    verifier = new EcdsaLicenseSignatureVerifier(spki, expectedKeyId);
}
catch (CryptographicException)
{
    Console.WriteLine("REJECTED:llave-publica-invalida");
    return 1;
}

var ok = verifier.Verify(parsed.KeyId, parsed.MachineCode, parsed.IssuedDate, parsed.Signature);
Console.WriteLine(ok ? "VERIFIED" : "REJECTED:firma");
return ok ? 0 : 1;
