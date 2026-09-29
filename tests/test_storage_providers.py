import unittest
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.auth import hash_password, require_auth, seed_initial_admin, verify_login, verify_password
from app.models.camera import Camera
from app.models.storage import StorageBackupFile, StorageConfig, SystemSettings
from app.models.user import SystemUser
from app.routers import system as system_router
from app.routers import users as users_router
from app.routers.system import build_provider_summary, resumir_erro_backup
from app.services import alert_service, backup_service, capture_service
from app.services.cloud_storage import (
    _google_error_message,
    _google_folder_for_remote_path,
    _upload_google_drive,
)
from app.tasks import scheduler as task_scheduler


class StorageProviderSummaryTests(unittest.TestCase):
    def test_smtp_password_is_encrypted_and_round_trips_with_system_secret(self):
        with patch.dict(os.environ, {'TIMELAPSE_SESSION_SECRET': 'test-session-secret'}, clear=True):
            encrypted = alert_service.criptografar_senha_smtp('smtp-test-password')
            decrypted = alert_service._descriptografar_senha_smtp(encrypted)

        self.assertNotEqual(encrypted, 'smtp-test-password')
        self.assertEqual(decrypted, 'smtp-test-password')

    def test_smtp_settings_route_stores_only_encrypted_password(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        payload = system_router.AlertSettingsRequest(
            smtp_enabled=True,
            smtp_host='smtp.example.test',
            smtp_port=587,
            smtp_username='sender@example.test',
            smtp_password='smtp-test-password',
            smtp_sender='sender@example.test',
            smtp_recipients='ops@example.test',
            smtp_security='starttls',
            alert_camera_failure=False,
        )
        with TestSession() as db, patch.dict(os.environ, {'TIMELAPSE_SESSION_SECRET': 'test-session-secret'}, clear=True):
            result = system_router.salvar_config_alertas(payload, db)
            settings = db.query(SystemSettings).filter_by(id=1).one()
            encrypted_password = settings.smtp_password_encrypted
        engine.dispose()

        self.assertTrue(result['configuracao']['smtp_password_configured'])
        self.assertFalse(result['configuracao']['alert_camera_failure'])
        self.assertNotIn('smtp_password', result['configuracao'])
        self.assertNotEqual(encrypted_password, 'smtp-test-password')
        with patch.dict(os.environ, {'TIMELAPSE_SESSION_SECRET': 'test-session-secret'}, clear=True):
            self.assertEqual(alert_service._descriptografar_senha_smtp(encrypted_password), 'smtp-test-password')

    def test_backup_alert_sends_with_starttls_and_enforces_cooldown(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with patch.dict(os.environ, {'TIMELAPSE_SESSION_SECRET': 'test-session-secret'}, clear=True):
            encrypted = alert_service.criptografar_senha_smtp('smtp-test-password')
        with TestSession() as db:
            db.add(SystemSettings(
                id=1,
                smtp_enabled=True,
                smtp_host='smtp.example.test',
                smtp_port=587,
                smtp_username='sender@example.test',
                smtp_password_encrypted=encrypted,
                smtp_sender='sender@example.test',
                smtp_recipients='ops@example.test',
                smtp_security='starttls',
                alert_backup_failure=True,
                alert_cooldown_seconds=1800,
            ))
            db.commit()
            with (
                patch.dict(os.environ, {'TIMELAPSE_SESSION_SECRET': 'test-session-secret'}, clear=True),
                patch('app.services.alert_service.smtplib.SMTP') as smtp_factory,
            ):
                first = alert_service.alertar_falha_backup(db, 'SMB de teste', 3)
                second = alert_service.alertar_falha_backup(db, 'SMB de teste', 3)

        engine.dispose()
        self.assertEqual(first, (True, ''))
        self.assertEqual(second, (False, 'Alerta dentro do período de espera.'))
        smtp_factory.assert_called_once_with('smtp.example.test', 587, timeout=20)
        smtp_factory.return_value.starttls.assert_called_once()
        smtp_factory.return_value.login.assert_called_once_with('sender@example.test', 'smtp-test-password')
        smtp_factory.return_value.send_message.assert_called_once()

    def test_camera_failure_alert_has_independent_per_camera_cooldown(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with TestSession() as db:
            first_camera = Camera(nome='Entrada', ip='192.0.2.1', usuario='user', senha='pass')
            second_camera = Camera(nome='Fundos', ip='192.0.2.2', usuario='user', senha='pass')
            db.add_all([
                SystemSettings(id=1, smtp_enabled=True, smtp_host='smtp.example.test', smtp_sender='sender@example.test', smtp_recipients='ops@example.test', alert_camera_failure=True, alert_cooldown_seconds=1800),
                first_camera,
                second_camera,
            ])
            db.commit()

            with patch('app.services.alert_service.enviar_email_configurado', return_value=(True, '')) as send_email:
                first = alert_service.alertar_falha_captura(db, first_camera.id, first_camera.nome)
                repeated = alert_service.alertar_falha_captura(db, first_camera.id, first_camera.nome)
                second = alert_service.alertar_falha_captura(db, second_camera.id, second_camera.nome)

        engine.dispose()
        self.assertEqual(first, (True, ''))
        self.assertEqual(repeated, (False, 'Alerta dentro do período de espera.'))
        self.assertEqual(second, (True, ''))
        self.assertEqual(send_email.call_count, 2)

    def test_scheduler_alerts_only_failed_attempts_and_clears_cooldown_on_success(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with TestSession() as db:
            camera = Camera(nome='Entrada', ip='192.0.2.1', usuario='user', senha='pass', last_capture_alert_at=123)
            db.add(camera)
            db.commit()
            camera_id = camera.id

            with (
                patch.object(task_scheduler, 'SessionLocal', TestSession),
                patch.object(task_scheduler, 'capturar_frame_camera', return_value=False),
                patch.object(task_scheduler, 'alertar_falha_captura') as alert,
            ):
                task_scheduler.job_captura_individual(camera_id, 'rtsp://camera/stream', 2)
                alert.assert_called_once()

            with (
                patch.object(task_scheduler, 'SessionLocal', TestSession),
                patch.object(task_scheduler, 'capturar_frame_camera', return_value=True),
                patch.object(task_scheduler, 'alertar_falha_captura') as alert,
            ):
                task_scheduler.job_captura_individual(camera_id, 'rtsp://camera/stream', 2)
                alert.assert_not_called()
                db.expire_all()
                self.assertEqual(db.get(Camera, camera_id).last_capture_alert_at, 0)

        engine.dispose()

    def test_auth_rejects_requests_when_credentials_are_not_configured(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with patch.dict(os.environ, {}, clear=True), TestSession() as db:
            request = SimpleNamespace(url=SimpleNamespace(path='/'), session={}, state=SimpleNamespace())
            with self.assertRaises(HTTPException) as error:
                require_auth(request, db)

        engine.dispose()
        self.assertEqual(error.exception.status_code, 503)

    def test_auth_accepts_only_configured_credentials(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with patch.dict(os.environ, {
            'TIMELAPSE_USERNAME': 'admin',
            'TIMELAPSE_PASSWORD': 'private-test-pass',
        }, clear=True), TestSession() as db:
            admin = seed_initial_admin(db)
            self.assertEqual(admin.role, 'admin')
            self.assertIsNotNone(verify_login('admin', 'private-test-pass', db))
            self.assertIsNone(verify_login('admin', 'wrong', db))
            self.assertIsNone(seed_initial_admin(db))
        engine.dispose()

    def test_auth_reads_password_from_systemd_credentials_directory(self):
        with tempfile.TemporaryDirectory() as credentials_directory:
            password_path = os.path.join(credentials_directory, 'TIMELAPSE_PASSWORD')
            with open(password_path, 'w', encoding='utf-8') as credential_file:
                credential_file.write('systemd-test-password\n')
            with patch.dict(os.environ, {
                'TIMELAPSE_USERNAME': 'admin',
                'TIMELAPSE_PASSWORD': '',
                'CREDENTIALS_DIRECTORY': credentials_directory,
            }, clear=True):
                engine = create_engine('sqlite:///:memory:')
                Base.metadata.create_all(engine)
                TestSession = sessionmaker(bind=engine)
                with TestSession() as db:
                    seed_initial_admin(db)
                    self.assertIsNotNone(verify_login('admin', 'systemd-test-password', db))
                engine.dispose()

    def test_auth_redirects_pages_and_rejects_api_requests_without_session(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with TestSession() as db:
            db.add(SystemUser(username='admin', password_hash=hash_password('configured-password'), role='admin', active=True))
            db.commit()
            request = SimpleNamespace(url=SimpleNamespace(path='/'), session={}, state=SimpleNamespace())
            api_request = SimpleNamespace(url=SimpleNamespace(path='/api/status'), session={}, state=SimpleNamespace())
            with self.assertRaises(HTTPException) as page_error:
                require_auth(request, db)
            with self.assertRaises(HTTPException) as api_error:
                require_auth(api_request, db)
            authorized_request = SimpleNamespace(url=SimpleNamespace(path='/'), session={'user_id': 1, 'auth_version': 0}, state=SimpleNamespace())
            self.assertEqual(require_auth(authorized_request, db).username, 'admin')

        engine.dispose()
        self.assertEqual(page_error.exception.status_code, 303)
        self.assertEqual(page_error.exception.headers['Location'], '/login')
        self.assertEqual(api_error.exception.status_code, 401)

    def test_scrypt_password_hash_verifies_and_rejects_wrong_password(self):
        encoded = hash_password('a-long-test-password')
        self.assertTrue(verify_password('a-long-test-password', encoded))
        self.assertFalse(verify_password('wrong-password', encoded))

    def test_admin_creates_common_user_with_hashed_password(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with TestSession() as db:
            admin = SystemUser(username='admin', password_hash=hash_password('admin-test-password'), role='admin', active=True)
            db.add(admin)
            db.commit()
            request = users_router.UserCreateRequest(
                username='operator1',
                password='operator-test-password',
                recovery_email='operator@example.test',
            )

            result = users_router.criar_usuario(request, db, admin)
            user = db.query(SystemUser).filter_by(username='operator1').one()

            self.assertEqual(result['role'], 'user')
            self.assertNotIn('password_hash', result)
            self.assertNotEqual(user.password_hash, 'operator-test-password')
            self.assertTrue(verify_password('operator-test-password', user.password_hash))
            with self.assertRaises(HTTPException) as error:
                users_router.require_admin(user)

        engine.dispose()
        self.assertEqual(error.exception.status_code, 403)

    def test_cannot_disable_the_last_active_admin(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with TestSession() as db:
            admin = SystemUser(username='admin', password_hash=hash_password('admin-test-password'), role='admin', active=True)
            db.add(admin)
            db.commit()
            request = users_router.UserUpdateRequest(active=False)

            with self.assertRaises(HTTPException) as error:
                users_router.atualizar_usuario(admin.id, request, db, admin)

        engine.dispose()
        self.assertEqual(error.exception.status_code, 409)

    def test_admin_cannot_change_own_profile_when_another_admin_exists(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with TestSession() as db:
            admin = SystemUser(username='admin', password_hash=hash_password('admin-test-password'), role='admin', active=True)
            other_admin = SystemUser(username='admin2', password_hash=hash_password('other-admin-test-password'), role='admin', active=True)
            db.add_all([admin, other_admin])
            db.commit()

            with self.assertRaises(HTTPException) as error:
                users_router.atualizar_usuario(admin.id, users_router.UserUpdateRequest(role='user'), db, admin)

            self.assertEqual(db.get(SystemUser, admin.id).role, 'admin')

        engine.dispose()
        self.assertEqual(error.exception.status_code, 409)

    def test_build_provider_summary_returns_local_style_fields(self):
        item = build_provider_summary(
            nome='Google Drive',
            total_bytes=200 * 1024 * 1024,
            usado_bytes=50 * 1024 * 1024,
            ativo=True,
        )

        self.assertEqual(item['nome'], 'Google Drive')
        self.assertEqual(item['ativo'], True)
        self.assertGreater(item['total_bytes'], 0)
        self.assertEqual(item['percentagem_uso'], 25.0)
        self.assertIn('total_bytes', item)
        self.assertIn('livre_bytes', item)

    def test_storage_status_returns_before_background_metrics_scan(self):
        with (
            patch.object(system_router, '_STORAGE_STATUS_CACHE', None),
            patch.object(system_router, '_STORAGE_STATUS_UPDATED_AT', 0.0),
            patch.object(system_router, '_STORAGE_STATUS_REFRESHING', False),
            patch('app.routers.system.shutil.disk_usage', return_value=(1000, 400, 600)),
            patch('app.routers.system._calcular_status_armazenamento') as calculate,
            patch('app.routers.system.threading.Thread') as thread,
        ):
            response = system_router.obter_status_armazenamento()

        calculate.assert_not_called()
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()
        self.assertEqual(response['metricas_status'], 'atualizando')
        self.assertIsNone(response['calculo_previo']['capturas_realizadas'])

    def test_backup_error_summary_does_not_expose_technical_details(self):
        message = resumir_erro_backup(
            '403 Client Error: Forbidden for url: https://drive.example/upload?token=secret'
        )

        self.assertIn('arquivos pendentes', message)
        self.assertNotIn('403', message)
        self.assertNotIn('https://', message)
        self.assertNotIn('secret', message)

    def test_count_frames_saved_per_storage_excludes_failed_and_video_records(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with TestSession() as db:
            storage = StorageConfig(tipo='smb')
            db.add(storage)
            db.flush()
            db.add_all([
                StorageBackupFile(
                    storage_config_id=storage.id,
                    local_path='Capturas/camera_1/frame_1.jpg',
                    remote_path='backup/frame_1.jpg',
                    file_size=100,
                    file_mtime_ns=1,
                    backed_up_at=1,
                    last_error='',
                ),
                StorageBackupFile(
                    storage_config_id=storage.id,
                    local_path='Capturas/camera_1/frame_2.jpg',
                    remote_path='backup/frame_2.jpg',
                    file_size=100,
                    file_mtime_ns=2,
                    backed_up_at=0,
                    last_error='upload failed',
                ),
                StorageBackupFile(
                    storage_config_id=storage.id,
                    local_path='Videos/camera_1/timelapse.mp4',
                    remote_path='backup/timelapse.mp4',
                    file_size=100,
                    file_mtime_ns=3,
                    backed_up_at=1,
                    last_error='',
                ),
            ])
            db.commit()

            count = system_router.contar_frames_no_backup(db, storage.id)

        engine.dispose()
        self.assertEqual(count, 1)

    def test_cleanup_removes_only_old_remote_verified_local_files(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        now = 2_000_000_000
        old_exists_after = True
        recent_exists_after = False
        with tempfile.TemporaryDirectory() as root, TestSession() as db:
            capture_dir = os.path.join(root, 'Capturas', 'camera_1')
            os.makedirs(capture_dir)
            old_path = os.path.join(capture_dir, 'frame_old.jpg')
            unverified_path = os.path.join(capture_dir, 'frame_unverified.jpg')
            recent_path = os.path.join(capture_dir, 'frame_recent.jpg')
            for path, mtime in (
                (unverified_path, now - 45 * 86400),
                (old_path, now - 31 * 86400),
                (recent_path, now - 2 * 86400),
            ):
                with open(path, 'wb') as output:
                    output.write(b'frame')
                os.utime(path, (mtime, mtime))

            storage_smb = StorageConfig(tipo='smb', ativo=True)
            storage_drive = StorageConfig(tipo='google_drive', ativo=True)
            db.add_all([storage_smb, storage_drive])
            db.flush()
            storages = [storage_smb, storage_drive]
            for path in (old_path, unverified_path, recent_path):
                stat = os.stat(path)
                for storage in storages:
                    db.add(StorageBackupFile(
                        storage_config_id=storage.id,
                        local_path=os.path.relpath(path, root).replace(os.sep, '/'),
                        remote_path=f'backup/{os.path.basename(path)}',
                        remote_id=f'{storage.id}-{os.path.basename(path)}',
                        file_size=stat.st_size,
                        file_mtime_ns=stat.st_mtime_ns,
                        backed_up_at=1,
                        last_error='',
                    ))
            db.commit()

            def fake_disk_usage(_):
                free = 100 if os.path.exists(old_path) else 150
                return SimpleNamespace(total=1000, used=1000 - free, free=free)

            verifications = []

            def verify_destination(record, destination):
                verifications.append((record.storage_config_id, destination.id, record.remote_id))
                return record.local_path.endswith('frame_old.jpg')

            result = backup_service.limpar_capturas_backupeadas(
                db,
                root=root,
                now=now,
                disk_usage=fake_disk_usage,
                verifier=verify_destination,
            )
            remaining_records = db.query(StorageBackupFile).count()
            old_exists_after = os.path.exists(old_path)
            unverified_exists_after = os.path.exists(unverified_path)
            recent_exists_after = os.path.exists(recent_path)

        engine.dispose()
        self.assertFalse(old_exists_after)
        self.assertTrue(unverified_exists_after)
        self.assertTrue(recent_exists_after)
        self.assertEqual(remaining_records, 6)
        self.assertEqual(result['arquivos_removidos'], 1)
        self.assertFalse(result['limite_atingido'])
        old_verifications = [item for item in verifications if item[2].endswith('frame_old.jpg')]
        self.assertEqual(
            old_verifications,
            [
                (storage_smb.id, storage_smb.id, f'{storage_smb.id}-frame_old.jpg'),
                (storage_drive.id, storage_drive.id, f'{storage_drive.id}-frame_old.jpg'),
            ],
        )

    def test_capture_is_skipped_when_disk_has_five_percent_free(self):
        usage = SimpleNamespace(total=1000, used=960, free=40)
        with (
            patch('app.services.capture_service.shutil.disk_usage', return_value=usage),
            patch('app.services.capture_service.subprocess.run') as ffmpeg,
        ):
            captured = capture_service.capturar_frame_camera(99, 'rtsp://camera/stream', 2)

        self.assertIsNone(captured)
        ffmpeg.assert_not_called()

    def test_scheduler_is_singleton_between_modules(self):
        self.assertIs(system_router.app_scheduler, task_scheduler.scheduler)

    def test_google_error_message_omits_resumable_session_url(self):
        response = SimpleNamespace(
            status_code=403,
            reason='Forbidden',
            json=lambda: {
                'error': {
                    'message': 'Rate limit exceeded',
                    'errors': [{'reason': 'userRateLimitExceeded'}],
                }
            },
        )

        message = _google_error_message(response)

        self.assertIn('userRateLimitExceeded', message)
        self.assertIn('Rate limit exceeded', message)
        self.assertNotIn('upload_id', message)

    def test_google_drive_update_does_not_send_read_only_parents(self):
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(b'frame')
            source_path = source.name

        try:
            file_list = Mock(ok=True)
            file_list.json.return_value = {'files': [{'id': 'existing-id'}]}
            upload_session = Mock(ok=True)
            upload_session.headers = {'Location': 'https://upload.example/session'}
            uploaded = Mock(status_code=200)
            uploaded.json.return_value = {'id': 'existing-id', 'size': 5}

            with (
                patch('app.services.cloud_storage._google_folder_for_remote_path', return_value='folder-id'),
                patch('app.services.cloud_storage.requests.get', return_value=file_list),
                patch('app.services.cloud_storage.requests.patch', return_value=upload_session) as patch_request,
                patch('app.services.cloud_storage.requests.put', return_value=uploaded),
            ):
                _upload_google_drive(
                    source_path,
                    SimpleNamespace(caminho_remoto='/TimeLapse'),
                    'token',
                    'TimeLapseBackup/Capturas/camera_1/frame.jpg',
                )
        finally:
            os.unlink(source_path)

        self.assertNotIn('parents', patch_request.call_args.kwargs['json'])

    def test_backup_does_not_start_while_another_run_holds_lock(self):
        with backup_service.BACKUP_LOCK:
            result = backup_service.executar_backup_integral(None)

        self.assertFalse(result['sucesso'])
        self.assertIn('em andamento', result['erros'][0])

    def test_google_drive_remote_path_resolves_configured_folder_from_root(self):
        config = SimpleNamespace(caminho_remoto='/TimeLapse')
        with patch(
            'app.services.cloud_storage._find_or_create_google_folder',
            side_effect=[
                'base-id', 'backup-id', 'captures-id', 'camera-id',
                'base-id', 'backup-id', 'captures-id', 'camera-id',
            ],
        ) as find_folder:
            folder_id = _google_folder_for_remote_path(
                config,
                'TimeLapseBackup/Capturas/camera_1/frame.jpg',
                'token',
            )
            _google_folder_for_remote_path(
                config,
                'TimeLapseBackup/Capturas/camera_1/next_frame.jpg',
                'token',
            )

        self.assertEqual(folder_id, 'camera-id')
        self.assertEqual(
            find_folder.call_args_list,
            [
                call('TimeLapse', 'root', 'token'),
                call('TimeLapseBackup', 'base-id', 'token'),
                call('Capturas', 'backup-id', 'token'),
                call('camera_1', 'captures-id', 'token'),
            ],
        )


if __name__ == '__main__':
    unittest.main()
