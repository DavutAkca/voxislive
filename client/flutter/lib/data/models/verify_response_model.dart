import '../../domain/entities/verify_result.dart';
import 'quota_model.dart';

class VerifyResponseModel extends VerifyResult {
  const VerifyResponseModel({
    required super.userId,
    required super.licenseId,
    required super.tier,
    required super.allowedMinutes,
    required super.usedMinutes,
    required super.remaining,
    required super.unlimited,
  });

  /// Defensive parse: a malformed body from the auth-core service must surface
  /// as a [ServerFailure], never as an uncaught cast/type error in the caller.
  factory VerifyResponseModel.fromJson(Object? data) {
    final json = JsonParse.asMap(data, 'verify response');
    return VerifyResponseModel(
      userId:         json.str('user_id'),
      licenseId:      json.str('license_id'),
      tier:           json.str('tier'),
      allowedMinutes: json.dbl('allowed_minutes'),
      usedMinutes:    json.dbl('used_minutes'),
      remaining:      json.dbl('remaining'),
      unlimited:      json.boolean('unlimited'),
    );
  }
}
