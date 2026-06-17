/// Public surface of the Voxis client network layer.
///
/// Import this single barrel to consume the auth/quota API:
/// `import 'package:voxis_client/voxis_client.dart';` then reach the wired
/// use cases through `VoxisDi.instance`.
library voxis_client;

export 'core/error/failures.dart';
export 'core/storage/secure_storage.dart';
export 'di.dart';
export 'domain/entities/quota.dart';
export 'domain/entities/verify_result.dart';
export 'domain/repositories/auth_repository.dart';
export 'domain/usecases/get_quota.dart';
export 'domain/usecases/report_usage.dart';
export 'domain/usecases/verify_token.dart';
