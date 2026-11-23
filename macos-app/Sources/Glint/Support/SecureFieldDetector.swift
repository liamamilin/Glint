import Foundation

/// Pure sensitivity classifier adapted from Cotabby's SecureFieldDetector.
/// Blocks password fields, OTP/CVV inputs, API keys, and localized secret labels
/// so sensitive text never enters the local model or usage.db.
enum SecureFieldDetector {
    static func isSecure(
        role: String?,
        subrole: String?,
        roleDescription: String?,
        title: String?,
        descriptionLabel: String?
    ) -> Bool {
        isSecureRole(role: role, subrole: subrole)
            || hasSecureLabel(roleDescription: roleDescription, title: title, descriptionLabel: descriptionLabel)
    }

    static func isSecureRole(role: String?, subrole: String?) -> Bool {
        containsSensitiveMarker([role, subrole])
    }

    static func hasSecureLabel(roleDescription: String?, title: String?, descriptionLabel: String?) -> Bool {
        containsSensitiveMarker([roleDescription, title, descriptionLabel])
            || [title, descriptionLabel].contains { $0.map(labelNamesSecret) ?? false }
    }

    private static func containsSensitiveMarker(_ markers: [String?]) -> Bool {
        markers
            .compactMap { $0?.lowercased() }
            .filter { !$0.isEmpty }
            .contains { marker in sensitiveMarkers.contains { marker.contains($0) } }
    }

    private static let sensitiveMarkers: [String] = [
        "secure",
        "password",
        "passcode",
        "passphrase",
        "cvv",
        "cvc",
        "security code",
        "verification code",
        "one-time code",
        "one time code",
        "social security",
        "card number",
        "credit card"
    ]

    private static let secretLabelSubstrings: [String] = [
        "密码", "密碼",
        "パスワード",
        "비밀번호",
        "passwort", "kennwort",
        "mot de passe",
        "contraseña",
        "wachtwoord",
        "lösenord"
    ]

    private static let secretLabelWordStarts: [String] = [
        "api key",
        "secret key",
        "secret access key",
        "access token",
        "private key",
        "recovery phrase",
        "seed phrase",
        "ssn"
    ]

    private static let labelMatchOptions: String.CompareOptions = [.caseInsensitive, .diacriticInsensitive]

    private static func labelNamesSecret(_ label: String) -> Bool {
        secretLabelSubstrings.contains { label.range(of: $0, options: labelMatchOptions) != nil }
            || secretLabelWordStarts.contains { startsWord($0, in: label) }
    }

    private static func startsWord(_ term: String, in label: String) -> Bool {
        var searchRange = label.startIndex..<label.endIndex
        while let found = label.range(of: term, options: labelMatchOptions, range: searchRange) {
            guard found.lowerBound > label.startIndex else { return true }
            let scalars = label.unicodeScalars
            let previous = scalars[scalars.index(before: found.lowerBound)]
            if !CharacterSet.alphanumerics.contains(previous) {
                return true
            }
            searchRange = found.upperBound..<label.endIndex
        }
        return false
    }
}
