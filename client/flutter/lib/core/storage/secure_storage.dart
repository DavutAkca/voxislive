import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Typed wrapper around flutter_secure_storage. Keys are namespaced under
/// 'voxis.' to avoid collisions with other packages.
class SecureStorage {
  // v10 default Android cipher is RSA OAEP + AES-GCM (the deprecated Jetpack
  // `encryptedSharedPreferences` path was removed). migrateOnAlgorithmChange
  // defaults to true, so JWT/email written by 9.x migrate automatically.
  SecureStorage._() : _store = const FlutterSecureStorage(
    aOptions: AndroidOptions(),
    iOptions: IOSOptions(accessibility: KeychainAccessibility.first_unlock),
  );

  static final SecureStorage instance = SecureStorage._();

  static const _kJwt         = 'voxis.pb_jwt';
  static const _kUserEmail   = 'voxis.user_email';

  final FlutterSecureStorage _store;

  Future<void> saveJwt(String token) => _store.write(key: _kJwt, value: token);

  Future<String?> readJwt() => _store.read(key: _kJwt);

  Future<void> deleteJwt() => _store.delete(key: _kJwt);

  Future<void> saveEmail(String email) =>
      _store.write(key: _kUserEmail, value: email);

  Future<String?> readEmail() => _store.read(key: _kUserEmail);

  /// Removes only Voxis-owned keys. Avoids deleteAll() so we never wipe secrets
  /// other packages may store in the same keychain/keystore.
  Future<void> clearAll() async {
    await _store.delete(key: _kJwt);
    await _store.delete(key: _kUserEmail);
  }
}
