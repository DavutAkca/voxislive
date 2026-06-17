import 'quota.dart';

/// Result of /auth/verify: the quota snapshot plus the verify-only identity
/// fields (user/license ids). Sentinel handling and display formatting are
/// inherited from [QuotaState] so they cannot drift between the two surfaces.
class VerifyResult extends QuotaState {
  const VerifyResult({
    required this.userId,
    required this.licenseId,
    required super.tier,
    required super.allowedMinutes,
    required super.usedMinutes,
    required super.remaining,
    required super.unlimited,
  });

  final String userId;
  final String licenseId;

  @override
  List<Object?> get props => [userId, licenseId, ...super.props];
}
