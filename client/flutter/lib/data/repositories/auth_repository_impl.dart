import 'dart:developer' as developer;

import '../../core/error/failures.dart';
import '../../core/storage/secure_storage.dart';
import '../../domain/entities/quota.dart';
import '../../domain/entities/verify_result.dart';
import '../../domain/repositories/auth_repository.dart';
import '../datasources/auth_remote_datasource.dart';

/// Bridges the domain layer to the remote datasource.
///
/// verifyToken relies on the JWT stored at login; the 401 interceptor in
/// ApiClient evicts the JWT on expiry. getQuota is a pure network fetch
/// against the Go service cache. reportUsage is fire-and-forget; callers
/// decide whether to surface a non-fatal Err to the user.
class AuthRepositoryImpl implements AuthRepository {
  const AuthRepositoryImpl(this._remote, this._storage);

  final AuthRemoteDataSource _remote;
  final SecureStorage _storage;

  @override
  Future<Result<String>> login({
    required String email,
    required String password,
  }) =>
      _guard(() => _remote.login(email: email, password: password));

  @override
  Future<Result<String>> register({
    required String email,
    required String password,
  }) =>
      _guard(() => _remote.register(email: email, password: password));

  @override
  Future<Result<VerifyResult>> verifyToken() async {
    final stored = await _storage.readJwt();
    if (stored == null || stored.isEmpty) {
      return const Err(UnauthorizedFailure('No stored JWT — user must log in'));
    }
    return _guard(() async {
      final model = await _remote.verifyToken();
      return model;
    }, onFailure: (f) async {
      // Only an expired/invalid token warrants eviction. A LicenseInactiveFailure
      // (403) means the JWT is still valid but billing state changed — keep it.
      if (f is UnauthorizedFailure) {
        await _storage.deleteJwt();
      }
    });
  }

  @override
  Future<Result<Quota>> getQuota() =>
      _guard(() async => await _remote.getQuota());

  @override
  Future<Result<void>> reportUsage({
    required String sessionId,
    required double deltaMinutes,
    required String source,
  }) =>
      // Map the void result through Ok(null); a bare void expression cannot be
      // passed as an Ok argument, so wrap it explicitly.
      _guard<void>(() async {
        await _remote.reportUsage(
          sessionId:    sessionId,
          deltaMinutes: deltaMinutes,
          source:       source,
        );
        return null;
      });

  /// Runs [action], mapping known [Failure]s through and collapsing any
  /// unexpected error into an [UnknownFailure] with a generic user-facing
  /// message. Raw detail goes only to the developer log, never to the UI.
  Future<Result<T>> _guard<T>(
    Future<T> Function() action, {
    Future<void> Function(Failure)? onFailure,
  }) async {
    try {
      return Ok(await action());
    } on Failure catch (f) {
      await onFailure?.call(f);
      return Err(f);
    } catch (e, st) {
      developer.log('Unexpected repository error', error: e, stackTrace: st);
      return const Err(UnknownFailure());
    }
  }
}
