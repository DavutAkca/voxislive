import 'package:equatable/equatable.dart';

/// Canonical license/quota snapshot shared by the /auth/quota and /auth/verify
/// surfaces. [VerifyResult] aliases this and adds the verify-only identity
/// fields, so sentinel handling and display formatting live in exactly one
/// place.
class QuotaState extends Equatable {
  const QuotaState({
    required this.tier,
    required this.allowedMinutes,
    required this.usedMinutes,
    required this.remaining,
    required this.unlimited,
  });

  final String tier;
  final double allowedMinutes;
  final double usedMinutes;

  /// Server sends -1.0 as the unlimited sentinel; treat any negative value as
  /// unlimited rather than as a usable balance. Read [effectiveUnlimited]
  /// instead of inspecting this directly.
  final double remaining;
  final bool unlimited;

  /// Single source of truth for the unlimited decision: either the explicit
  /// flag or the negative-remaining sentinel.
  bool get effectiveUnlimited => unlimited || remaining < 0;

  bool get hasQuota => effectiveUnlimited || remaining > 0;

  double get remainingFraction {
    if (effectiveUnlimited || allowedMinutes <= 0) return 1.0;
    return (remaining / allowedMinutes).clamp(0.0, 1.0);
  }

  String get displayRemaining {
    if (effectiveUnlimited) return '∞';
    final mins = remaining.clamp(0.0, double.infinity);
    final h = (mins / 60).floor();
    final m = (mins % 60).floor();
    return h > 0 ? '${h}h ${m}m' : '${m}m';
  }

  @override
  List<Object?> get props =>
      [tier, allowedMinutes, usedMinutes, remaining, unlimited];
}

/// Retained name for the quota-only surface (getQuota). Identical shape to
/// [QuotaState]; existing call sites keep compiling.
class Quota extends QuotaState {
  const Quota({
    required super.tier,
    required super.allowedMinutes,
    required super.usedMinutes,
    required super.remaining,
    required super.unlimited,
  });
}
