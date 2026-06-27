import 'package:dio/dio.dart';

import '../storage/secure_storage.dart';

const _baseUrl = 'https://voxislive.com';
const _connectTimeout = Duration(seconds: 8);
const _receiveTimeout = Duration(seconds: 10);

/// Central Dio instance. Injects the stored JWT, evicts it on 401, and applies
/// consistent timeouts and JSON content-type.
class ApiClient {
  ApiClient._() {
    // No custom validateStatus: let Dio throw DioException on >=400 so the
    // datasource can map status codes (401/402/403/5xx) and transport errors
    // (timeouts, connection failures) to distinct Failure subtypes.
    _dio = Dio(BaseOptions(
      baseUrl: _baseUrl,
      connectTimeout: _connectTimeout,
      receiveTimeout: _receiveTimeout,
      headers: {'Content-Type': 'application/json'},
    ));
    _dio.interceptors.addAll([_AuthInterceptor(), _LogInterceptor()]);
  }

  static final ApiClient instance = ApiClient._();
  late final Dio _dio;

  Dio get dio => _dio;
}

class _AuthInterceptor extends Interceptor {
  @override
  Future<void> onRequest(
    RequestOptions options,
    RequestInterceptorHandler handler,
  ) async {
    final jwt = await SecureStorage.instance.readJwt();
    if (jwt != null && jwt.isNotEmpty) {
      options.headers['Authorization'] = 'Bearer $jwt';
    }
    handler.next(options);
  }

  @override
  Future<void> onError(
    DioException err,
    ErrorInterceptorHandler handler,
  ) async {
    // With the default validateStatus, a 401 now arrives here as an error
    // response. Evict the stored JWT so the next verify obtains a fresh token
    // instead of looping with stale credentials. A 403 (license inactive) is
    // deliberately NOT evicted — the token is still valid; only billing state
    // changed.
    final path = err.requestOptions.path;
    final isAuthEndpoint =
        path.contains('/auth/login') || path.contains('/auth/register');
    // A 401 on verify/quota/usage means the stored token is stale → evict it. A
    // 401 from login/register is a bad-credentials response, not a stale stored
    // token, so it must NOT wipe an existing valid JWT.
    if (err.response?.statusCode == 401 && !isAuthEndpoint) {
      await SecureStorage.instance.deleteJwt();
    }
    handler.next(err);
  }
}

class _LogInterceptor extends Interceptor {
  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    // Authorization header is redacted; raw JWTs must never reach the logs.
    assert(() {
      final safe = Map<String, dynamic>.from(options.headers)
        ..update('Authorization', (_) => '[REDACTED]', ifAbsent: () => '—');
      // ignore: avoid_print
      print('→ ${options.method} ${options.path} headers=$safe');
      return true;
    }());
    handler.next(options);
  }

  @override
  void onResponse(Response response, ResponseInterceptorHandler handler) {
    assert(() {
      // ignore: avoid_print
      print('← ${response.statusCode} ${response.requestOptions.path}');
      return true;
    }());
    handler.next(response);
  }

  @override
  void onError(DioException err, ErrorInterceptorHandler handler) {
    // Error responses (>=400) and transport failures bypass onResponse; log the
    // status/type here. Body is omitted to avoid leaking tokens or PII.
    assert(() {
      final code = err.response?.statusCode;
      // ignore: avoid_print
      print('✗ ${code ?? err.type} ${err.requestOptions.path}');
      return true;
    }());
    handler.next(err);
  }
}
