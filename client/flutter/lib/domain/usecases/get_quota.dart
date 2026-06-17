import '../../core/error/failures.dart';
import '../entities/quota.dart';
import '../repositories/auth_repository.dart';

/// Retrieves the current quota from the Go service's in-process cache.
/// A lightweight poll with no PocketBase round-trip; requires [VerifyToken]
/// to have succeeded at least once this session. The returned [Quota] is a
/// [QuotaState], so unlimited/sentinel handling is read via its accessors
/// rather than re-derived by callers.
class GetQuota {
  const GetQuota(this._repo);
  final AuthRepository _repo;

  Future<Result<Quota>> call() => _repo.getQuota();
}
