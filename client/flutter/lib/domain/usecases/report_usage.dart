import '../../core/error/failures.dart';
import '../repositories/auth_repository.dart';

class ReportUsageParams {
  const ReportUsageParams({
    required this.sessionId,
    required this.deltaMinutes,
    required this.source,
  });

  final String sessionId;
  final double deltaMinutes;
  /// "video" | "meeting_incoming" | "meeting_outgoing"
  final String source;
}

/// Posts a usage delta to the Go backend at session teardown. Failures are
/// non-fatal from the UX perspective (the session has already ended); callers
/// should log [Err] results for billing reconciliation.
///
/// Transient transport failures buffer the delta in memory and replay it on the
/// next successful report, so a momentary network blip does not silently drop
/// billable minutes. Buffering is gated to [NetworkFailure] only — those are
/// transport-level errors where the request did not complete server-side, so a
/// replay cannot double-count. A [ServerFailure] (the server may have processed
/// the delta before erroring) and auth/quota failures are surfaced without
/// buffering. The buffer is process-local and not persisted across restarts;
/// durable, exactly-once delivery requires a server-side idempotency key on
/// /usage/report (MAINTAINER: add `report_id` dedup key there).
class ReportUsage {
  ReportUsage(this._repo);
  final AuthRepository _repo;

  /// Pending deltas keyed by "sessionId|source", accumulated across transient
  /// failures and flushed on the next successful send for the same key.
  final Map<String, _PendingDelta> _pending = {};

  Future<Result<void>> call(ReportUsageParams params) async {
    final key = '${params.sessionId}|${params.source}';
    final buffered = _pending.remove(key)?.minutes ?? 0.0;
    final total = params.deltaMinutes + buffered;

    final result = await _repo.reportUsage(
      sessionId:    params.sessionId,
      deltaMinutes: total,
      source:       params.source,
    );

    if (result case Err(:final failure)) {
      if (failure is NetworkFailure) {
        // Request never reached the server; retain the combined delta to retry on
        // the next report rather than losing the minutes.
        _pending[key] = _PendingDelta(params.sessionId, params.source, total);
      } else if (buffered > 0) {
        // A non-network failure may have been processed server-side, so the fresh
        // delta is not replayed (avoids double-count). But the previously-buffered
        // minutes came from earlier transport failures that never reached the
        // server — restore them so they are not silently dropped here.
        _pending[key] = _PendingDelta(params.sessionId, params.source, buffered);
      }
    }
    return result;
  }
}

class _PendingDelta {
  const _PendingDelta(this.sessionId, this.source, this.minutes);
  final String sessionId;
  final String source;
  final double minutes;
}
