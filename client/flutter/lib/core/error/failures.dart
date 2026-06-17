import 'package:equatable/equatable.dart';

sealed class Failure extends Equatable {
  const Failure(this.message);
  final String message;
  @override
  List<Object?> get props => [message];
}

final class NetworkFailure extends Failure {
  const NetworkFailure([super.message = 'Network unreachable']);
}

final class UnauthorizedFailure extends Failure {
  const UnauthorizedFailure([super.message = 'Invalid or expired token']);
}

final class QuotaExceededFailure extends Failure {
  const QuotaExceededFailure([super.message = 'Translation quota exceeded']);
}

/// 403 from auth-core: the JWT is valid but the license is inactive, expired, or
/// missing. Distinct from [UnauthorizedFailure] so callers never evict a good
/// token over a billing-state problem.
final class LicenseInactiveFailure extends Failure {
  const LicenseInactiveFailure([super.message = 'License inactive or not found']);
}

final class ServerFailure extends Failure {
  const ServerFailure([super.message = 'Server error']);
  factory ServerFailure.status(int code) => ServerFailure('Server returned $code');
}

/// Catch-all for unexpected, non-classified errors. Carries a generic
/// user-facing message; raw detail belongs in logs, not in [message].
final class UnknownFailure extends Failure {
  const UnknownFailure([super.message = 'Something went wrong']);
}

/// Functional Result type. Avoids adding dartz as a dependency.
sealed class Result<T> {
  const Result();
}

final class Ok<T> extends Result<T> {
  const Ok(this.value);
  final T value;
}

final class Err<T> extends Result<T> {
  const Err(this.failure);
  final Failure failure;
}

extension ResultX<T> on Result<T> {
  bool get isOk => this is Ok<T>;
  bool get isErr => this is Err<T>;

  /// Unwraps an [Ok]. Throws a descriptive [StateError] on [Err] so a
  /// programmer mistake surfaces clearly instead of a raw cast failure.
  T get unwrap => switch (this) {
        Ok(:final value) => value,
        Err(:final failure) =>
          throw StateError('unwrap called on Err: ${failure.message}'),
      };

  /// The failure of an [Err]. Throws a descriptive [StateError] on [Ok].
  Failure get error => switch (this) {
        Err(:final failure) => failure,
        Ok() => throw StateError('error called on Ok'),
      };

  /// Safe accessor: the value on [Ok], otherwise null.
  T? get valueOrNull => switch (this) {
        Ok(:final value) => value,
        Err() => null,
      };

  /// Safe accessor: the failure on [Err], otherwise null.
  Failure? get failureOrNull => switch (this) {
        Err(:final failure) => failure,
        Ok() => null,
      };

  /// The value on [Ok], or [fallback] derived from the failure on [Err].
  T getOrElse(T Function(Failure) fallback) => switch (this) {
        Ok(:final value) => value,
        Err(:final failure) => fallback(failure),
      };

  R fold<R>(R Function(T) ok, R Function(Failure) err) =>
      switch (this) {
        Ok(:final value) => ok(value),
        Err(:final failure) => err(failure),
      };
}
