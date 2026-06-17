import '../../core/error/failures.dart';
import '../../domain/entities/quota.dart';

class QuotaModel extends Quota {
  const QuotaModel({
    required super.tier,
    required super.allowedMinutes,
    required super.usedMinutes,
    required super.remaining,
    required super.unlimited,
  });

  /// Defensive parse: a malformed body must surface as a [ServerFailure], not an
  /// uncaught cast error. The remaining==-1 unlimited sentinel is interpreted by
  /// QuotaState (effectiveUnlimited), so it is stored verbatim here.
  factory QuotaModel.fromJson(Object? data) {
    final json = JsonParse.asMap(data, 'quota response');
    return QuotaModel(
      tier:           json.str('tier'),
      allowedMinutes: json.dbl('allowed_minutes'),
      usedMinutes:    json.dbl('used_minutes'),
      remaining:      json.dbl('remaining'),
      unlimited:      json.boolean('unlimited'),
    );
  }
}

/// Null-aware JSON readers shared by the response models. Any structural problem
/// (wrong root type, missing key, wrong field type) becomes a [ServerFailure]
/// describing the offending field, so the failure is diagnosable from logs.
extension JsonParse on Map<String, dynamic> {
  static Map<String, dynamic> asMap(Object? data, String context) {
    if (data is Map<String, dynamic>) return data;
    throw ServerFailure('Malformed $context: expected a JSON object');
  }

  String str(String key) {
    final v = this[key];
    if (v is String) return v;
    throw ServerFailure('Malformed response: field "$key" is missing or not a string');
  }

  double dbl(String key) {
    final v = this[key];
    if (v is num) return v.toDouble();
    throw ServerFailure('Malformed response: field "$key" is missing or not a number');
  }

  bool boolean(String key) {
    final v = this[key];
    if (v is bool) return v;
    throw ServerFailure('Malformed response: field "$key" is missing or not a boolean');
  }
}
