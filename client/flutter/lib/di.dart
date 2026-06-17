import 'core/network/api_client.dart';
import 'core/storage/secure_storage.dart';
import 'data/datasources/auth_remote_datasource.dart';
import 'data/repositories/auth_repository_impl.dart';
import 'domain/repositories/auth_repository.dart';
import 'domain/usecases/get_quota.dart';
import 'domain/usecases/report_usage.dart';
import 'domain/usecases/verify_token.dart';

/// Manual composition root. A hand-wired factory keeps the dependency graph
/// explicit and the package free of a service-locator runtime — the graph is
/// small enough that get_it would add cost without benefit.
///
/// Wiring is lazy and memoized: each getter builds its node once and reuses it,
/// so the singletons (ApiClient, SecureStorage) are shared across use cases.
class VoxisDi {
  VoxisDi._();

  static final VoxisDi instance = VoxisDi._();

  SecureStorage get storage => SecureStorage.instance;

  AuthRemoteDataSource get _remote =>
      _remoteCache ??= AuthRemoteDataSourceImpl(
        dio: ApiClient.instance.dio,
        storage: storage,
      );
  AuthRemoteDataSource? _remoteCache;

  AuthRepository get repository =>
      _repositoryCache ??= AuthRepositoryImpl(_remote, storage);
  AuthRepository? _repositoryCache;

  LoginUseCase get login => _loginCache ??= LoginUseCase(repository);
  LoginUseCase? _loginCache;

  RegisterUseCase get register =>
      _registerCache ??= RegisterUseCase(repository);
  RegisterUseCase? _registerCache;

  VerifyToken get verifyToken =>
      _verifyTokenCache ??= VerifyToken(repository);
  VerifyToken? _verifyTokenCache;

  GetQuota get getQuota => _getQuotaCache ??= GetQuota(repository);
  GetQuota? _getQuotaCache;

  // ReportUsage holds a per-instance retry buffer, so it must be a memoized
  // singleton; a fresh instance per call would discard buffered billing deltas.
  ReportUsage get reportUsage =>
      _reportUsageCache ??= ReportUsage(repository);
  ReportUsage? _reportUsageCache;
}
