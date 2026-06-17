import '../../core/error/failures.dart';
import '../entities/verify_result.dart';
import '../repositories/auth_repository.dart';

/// Verifies the locally cached PocketBase JWT against auth-core and returns
/// the current license state. Callers should invoke this before starting a
/// translation session to confirm quota availability.
///
/// Eviction policy is enforced in the repository: an [UnauthorizedFailure]
/// (401) evicts the stored token; a [LicenseInactiveFailure] (403) does not,
/// since the token is still valid and only billing state changed.
class VerifyToken {
  const VerifyToken(this._repo);
  final AuthRepository _repo;

  Future<Result<VerifyResult>> call() => _repo.verifyToken();
}

/// Authenticates an existing user and persists the session token.
class LoginUseCase {
  const LoginUseCase(this._repo);
  final AuthRepository _repo;

  Future<Result<String>> call({
    required String email,
    required String password,
  }) =>
      _repo.login(email: email, password: password);
}

/// Registers a new user and persists the session token.
class RegisterUseCase {
  const RegisterUseCase(this._repo);
  final AuthRepository _repo;

  Future<Result<String>> call({
    required String email,
    required String password,
  }) =>
      _repo.register(email: email, password: password);
}
