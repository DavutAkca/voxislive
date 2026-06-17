import 'package:dio/dio.dart';

import '../../core/error/failures.dart';
import '../../core/network/api_client.dart';
import '../../core/storage/secure_storage.dart';
import '../models/quota_model.dart';
import '../models/verify_response_model.dart';

/// HTTP calls to the Go auth-core service. All methods throw [Failure]
/// subtypes; the repository layer translates them into Result values.
abstract interface class AuthRemoteDataSource {
  /// Authenticates against PocketBase via auth-core, persisting the returned
  /// JWT and email. Returns the user's email on success.
  Future<String> login({required String email, required String password});

  /// Registers a new user, persisting the returned JWT and email. Returns the
  /// user's email on success.
  Future<String> register({required String email, required String password});

  Future<VerifyResponseModel> verifyToken();
  Future<QuotaModel> getQuota();
  Future<void> reportUsage({
    required String sessionId,
    required double deltaMinutes,
    required String source,
  });
}

class AuthRemoteDataSourceImpl implements AuthRemoteDataSource {
  AuthRemoteDataSourceImpl({Dio? dio, SecureStorage? storage})
      : _dio = dio ?? ApiClient.instance.dio,
        _storage = storage ?? SecureStorage.instance;

  final Dio _dio;
  final SecureStorage _storage;

  @override
  Future<String> login({required String email, required String password}) =>
      _authenticate('/auth/login', email: email, password: password);

  @override
  Future<String> register({required String email, required String password}) =>
      _authenticate('/auth/register', email: email, password: password);

  /// Shared login/register flow: posts credentials, extracts the JWT, and
  /// persists token + email atomically before returning.
  Future<String> _authenticate(
    String path, {
    required String email,
    required String password,
  }) async {
    try {
      final resp = await _dio.post(path, data: {
        'email': email,
        'password': password,
      });
      final body = resp.data;
      if (body is! Map<String, dynamic>) {
        throw const ServerFailure('Malformed auth response: expected a JSON object');
      }
      final token = body['token'];
      if (token is! String || token.isEmpty) {
        throw const ServerFailure('Auth response missing token');
      }
      await _storage.saveJwt(token);
      await _storage.saveEmail(email);
      return email;
    } on DioException catch (e) {
      throw _mapDioException(e);
    }
  }

  @override
  Future<VerifyResponseModel> verifyToken() async {
    try {
      final resp = await _dio.post('/auth/verify');
      return VerifyResponseModel.fromJson(resp.data);
    } on DioException catch (e) {
      throw _mapDioException(e);
    }
  }

  @override
  Future<QuotaModel> getQuota() async {
    try {
      final resp = await _dio.get('/auth/quota');
      return QuotaModel.fromJson(resp.data);
    } on DioException catch (e) {
      throw _mapDioException(
        e,
        unauthorizedMessage: 'Not verified — call verifyToken first',
      );
    }
  }

  @override
  Future<void> reportUsage({
    required String sessionId,
    required double deltaMinutes,
    required String source,
  }) async {
    if (deltaMinutes <= 0) return;
    try {
      final resp = await _dio.post('/usage/report', data: {
        'session_id':    sessionId,
        'delta_minutes': double.parse(deltaMinutes.toStringAsFixed(4)),
        'source':        source,
      });
      // Accept any 2xx: the handler may answer 200 or 204 for a fire-and-forget
      // heartbeat. Non-2xx with a body still reaches us via DioException.
      final code = resp.statusCode ?? 0;
      if (code < 200 || code >= 300) {
        throw ServerFailure.status(code);
      }
    } on DioException catch (e) {
      throw _mapDioException(e);
    }
  }

  /// Maps a [DioException] to the right [Failure].
  ///
  /// Transport problems (timeouts, connection drops, no route) are
  /// [NetworkFailure]; HTTP responses are classified by status code. 403 maps
  /// to [LicenseInactiveFailure] so callers never evict a valid token over a
  /// billing-state problem; 401 to [UnauthorizedFailure]; 5xx to
  /// [ServerFailure]. A [Failure] already thrown inside the try block (e.g. a
  /// parse failure) is rethrown unchanged.
  Failure _mapDioException(DioException e, {String? unauthorizedMessage}) {
    if (e.error is Failure) return e.error as Failure;

    switch (e.type) {
      case DioExceptionType.connectionTimeout:
      case DioExceptionType.receiveTimeout:
      case DioExceptionType.sendTimeout:
      case DioExceptionType.connectionError:
        return NetworkFailure(e.message ?? 'Network unreachable');
      case DioExceptionType.cancel:
        return NetworkFailure(e.message ?? 'Request cancelled');
      case DioExceptionType.badCertificate:
        return const NetworkFailure('TLS certificate rejected');
      case DioExceptionType.badResponse:
      case DioExceptionType.unknown:
        break;
    }

    final code = e.response?.statusCode;
    if (code == null) return NetworkFailure(e.message ?? 'Network error');
    return switch (code) {
      401 => UnauthorizedFailure(unauthorizedMessage ?? 'Invalid or expired token'),
      402 => const QuotaExceededFailure(),
      403 => const LicenseInactiveFailure(),
      >= 500 => ServerFailure.status(code),
      _ => ServerFailure.status(code),
    };
  }
}
