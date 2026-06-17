import '../entities/quota.dart';
import '../entities/verify_result.dart';
import '../../core/error/failures.dart';

abstract interface class AuthRepository {
  /// Authenticates an existing user; persists the JWT and email on success.
  /// Returns the user's email.
  Future<Result<String>> login({
    required String email,
    required String password,
  });

  /// Registers a new user; persists the JWT and email on success.
  /// Returns the user's email.
  Future<Result<String>> register({
    required String email,
    required String password,
  });

  /// Verifies the stored PocketBase JWT against auth-core. On 401 the JWT is
  /// evicted from SecureStorage by the ApiClient interceptor.
  Future<Result<VerifyResult>> verifyToken();

  /// Returns quota state from the Go service cache. Requires a prior
  /// successful verifyToken in the same session.
  Future<Result<Quota>> getQuota();

  /// Reports consumed minutes for the completed session.
  /// [source] is one of "video", "meeting_incoming", or "meeting_outgoing".
  Future<Result<void>> reportUsage({
    required String sessionId,
    required double deltaMinutes,
    required String source,
  });
}
