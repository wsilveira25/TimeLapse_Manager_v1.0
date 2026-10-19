import unittest
import os
import threading
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.auth import hash_password, require_auth, seed_initial_admin, verify_login, verify_password
from app.core.oauth_urls import obter_oauth_redirect_uri, validar_oauth_redirect_uri
from app.core.oauth_urls import validar_oauth_redirect_uri
from app.models.camera import Camera
from app.models.storage import LocalStorageLocation, StorageBackupFile, StorageConfig, SystemSettings
from app.models.user import SystemUser
from app.routers import system as system_router
from app.routers.system import DomainSettingsRequest, LocalStorageLocationRequest
from app.routers import users as users_router
from app.routers import cameras as cameras_router
from app.routers.system import build_provider_summary, resumir_erro_backup
from app.services import alert_service, backup_service, capture_service, cloud_storage, local_storage, video_service
from app.services.cloud_storage import (
    _google_error_message,
    _google_folder_for_remote_path,
    _upload_google_drive,
)
from app.tasks import scheduler as task_scheduler
from urllib3.util import connection as urllib3_connection


class StorageProviderSummaryTests(unittest.TestCase):
    def test_domain_settings_normalize_and_persist_valid_hostname(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            system_router,
            'DOMAIN_CONFIG_PATH',
            Path(directory).resolve() / 'domain.env',
        ), TestSession() as db:
            result = system_router.salvar_config_dominio(
                DomainSettingsRequest(domain_name='  Cam.Timelapse.Example.COM.  '),
                db,
            )
            self.assertEqual(result['domain_name'], 'cam.timelapse.example.com')
            settings = db.query(SystemSettings).filter_by(id=1).one()
            self.assertEqual(settings.domain_name, 'cam.timelapse.example.com')
        engine.dispose()

    def test_domain_settings_reject_url_ip_and_invalid_dns_labels(self):
        for domain_name in (
            'http://camera.example.com',
            'https://camera.example.com/path',
            '192.0.2.4',
            'camera..example.com',
            'camera.example.com:443',
        ):
            with self.subTest(domain_name=domain_name), self.assertRaises(HTTPException) as error:
                system_router.salvar_config_dominio(
                    DomainSettingsRequest(domain_name=domain_name),
                    Mock(),
                )
            self.assertEqual(error.exception.status_code, 422)

    def test_domain_settings_accepts_https_url_and_trailing_root_slash(self):
        self.assertEqual(
            system_router.normalizar_dominio('https://Jsilveira.sys.net/'),
            'jsilveira.sys.net',
        )

    def test_domain_provision_requires_initial_root_setup(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with TestSession() as db:
            db.add(SystemSettings(id=1, domain_name='camera.example.test'))
            db.commit()
            with (
                patch.object(system_router, 'DOMAIN_SETUP_UNIT_PATH', Path('/missing/domain.service')),
                patch.object(system_router, 'DOMAIN_SETUP_SUDOERS_PATH', Path('/missing/domain.sudoers')),
                self.assertRaises(HTTPException) as error,
            ):
                system_router.provisionar_certificado_dominio(db)
        engine.dispose()
        self.assertEqual(error.exception.status_code, 503)
        self.assertIn('Execute uma vez', error.exception.detail)

    def test_domain_provision_invokes_only_fixed_systemd_helper(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with tempfile.TemporaryDirectory() as directory, TestSession() as db:
            unit_path = Path(directory).resolve() / 'domain.service'
            sudoers_path = Path(directory).resolve() / 'domain.sudoers'
            unit_path.touch()
            sudoers_path.touch()
            db.add(SystemSettings(id=1, domain_name='camera.example.test'))
            db.commit()
            with (
                patch.object(system_router, 'DOMAIN_SETUP_UNIT_PATH', unit_path),
                patch.object(system_router, 'DOMAIN_SETUP_SUDOERS_PATH', sudoers_path),
                patch.object(system_router.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run,
            ):
                result = system_router.provisionar_certificado_dominio(db)
        engine.dispose()
        self.assertTrue(result['sucesso'])
        run.assert_called_once_with(
            [system_router.SUDO_PATH, '-n', system_router.SYSTEMCTL_PATH, '--no-block', 'restart', system_router.DOMAIN_SETUP_UNIT],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )

    def test_google_oauth_requires_drive_file_scope_in_granted_callback_scopes(self):
        self.assertTrue(system_router.google_drive_file_scope_concedido(
            'openid email https://www.googleapis.com/auth/drive.file'
        ))
        self.assertFalse(system_router.google_drive_file_scope_concedido('openid email'))
        self.assertFalse(system_router.google_drive_file_scope_concedido(''))

    def test_google_oauth_callback_clears_tokens_when_drive_scope_was_not_granted(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with TestSession() as db:
            config = StorageConfig(
                id=2,
                tipo='google_drive',
                oauth_state='state-value',
                oauth_access_token='old-access-token',
                oauth_refresh_token='old-refresh-token',
                oauth_token_expires_at=123456,
            )
            db.add(config)
            db.commit()
            response = system_router.concluir_oauth(
                request=Mock(),
                state='state-value',
                code='authorization-code',
                scope='openid email',
                db=db,
            )
            db.refresh(config)
            self.assertEqual(config.oauth_access_token, '')
            self.assertEqual(config.oauth_refresh_token, '')
            self.assertEqual(config.oauth_token_expires_at, 0)
            self.assertIn('drive.file', config.storage_error)
        engine.dispose()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/?storage_oauth=drive_scope_missing')

    def test_successful_google_quota_check_clears_stale_storage_error(self):
        config = SimpleNamespace(
            tipo='google_drive',
            oauth_client_id='client-id',
            oauth_client_secret='client-secret',
            oauth_refresh_token='refresh-token',
            oauth_access_token='access-token',
            oauth_token_expires_at=9999999999,
            storage_error='403 Client Error: Forbidden',
            total_bytes=0,
            usado_bytes=0,
            livre_bytes=0,
            percentagem_uso=0,
        )
        response = Mock(ok=True)
        response.json.return_value = {'storageQuota': {'limit': '1000', 'usage': '250'}}
        with patch('app.services.cloud_storage.requests.get', return_value=response):
            result = cloud_storage.get_storage_quota(config)

        self.assertEqual(result, {'total_bytes': 1000, 'usado_bytes': 250})
        self.assertEqual(config.storage_error, '')
        self.assertEqual(config.livre_bytes, 750)

    def test_validated_backup_record_clears_old_error_without_reupload(self):
        record = SimpleNamespace(
            backed_up_at=123456,
            file_size=256,
            file_mtime_ns=987654,
            last_error='old duplicate insert error',
        )
        file_stat = SimpleNamespace(st_size=256, st_mtime_ns=987654)

        self.assertTrue(backup_service._backup_record_matches_file(record, file_stat))
        self.assertEqual(record.last_error, '')
        self.assertFalse(backup_service._backup_record_matches_file(record, SimpleNamespace(st_size=257, st_mtime_ns=987654)))
        self.assertFalse(backup_service._backup_record_matches_file(None, file_stat))

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

    def test_cloud_http_prefers_ipv4_when_configured(self):
        original_has_ipv6 = urllib3_connection.HAS_IPV6
        try:
            with patch.dict(os.environ, {'TIMELAPSE_HTTP_IPV4_ONLY': 'true'}):
                cloud_storage._configure_http_address_family()
                self.assertFalse(urllib3_connection.HAS_IPV6)

            with patch.dict(os.environ, {'TIMELAPSE_HTTP_IPV4_ONLY': 'false'}):
                cloud_storage._configure_http_address_family()
                self.assertTrue(urllib3_connection.HAS_IPV6)
        finally:
            urllib3_connection.HAS_IPV6 = original_has_ipv6

    def test_oauth_redirect_uri_requires_verified_dns_and_https(self):
        with self.assertRaisesRegex(ValueError, 'domínio DNS'):
            validar_oauth_redirect_uri('https://191.177.235.73:8081/api/system/storage/oauth/callback')
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            validar_oauth_redirect_uri('http://timelapse.example.com/api/system/storage/oauth/callback')
        with self.assertRaisesRegex(ValueError, 'exatamente'):
            validar_oauth_redirect_uri('https://timelapse.example.com/other')

        valid_uri = 'https://timelapse.example.com/api/system/storage/oauth/callback'
        validar_oauth_redirect_uri(valid_uri)
        request = Mock()
        request.url_for.return_value = 'https://191.177.235.73:8081/api/system/storage/oauth/callback'
        with patch.dict(os.environ, {'TIMELAPSE_OAUTH_REDIRECT_URI': valid_uri}):
            self.assertEqual(obter_oauth_redirect_uri(request), valid_uri)

    def test_google_drive_oauth_requests_only_file_scoped_drive_access(self):
        scopes = cloud_storage._provider_config('google_drive')['scopes'].split()
        self.assertIn('https://www.googleapis.com/auth/drive.file', scopes)
        self.assertIn('openid', scopes)
        self.assertIn('email', scopes)
        self.assertNotIn('https://www.googleapis.com/auth/drive.metadata.readonly', scopes)

    def test_oauth_redirect_uri_requires_dns_https_for_public_hosts(self):
        with self.assertRaisesRegex(ValueError, 'Configure um domínio DNS'):
            validar_oauth_redirect_uri('https://191.177.235.73:8081/api/system/storage/oauth/callback')

        validar_oauth_redirect_uri('https://timelapse.example.com/api/system/storage/oauth/callback')
        validar_oauth_redirect_uri('http://localhost:8000/api/system/storage/oauth/callback')

        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            validar_oauth_redirect_uri('http://timelapse.example.com/api/system/storage/oauth/callback')

    def test_storage_status_returns_before_background_metrics_scan(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with TestSession() as db:
            db.add(StorageConfig(tipo='smb', nome='SMB da obra', ativo=True, host='192.168.0.6', caminho_remoto='capturas'))
            db.commit()
            with (
                patch.object(system_router, '_STORAGE_STATUS_CACHE', None),
                patch.object(system_router, '_STORAGE_STATUS_UPDATED_AT', 0.0),
                patch.object(system_router, '_STORAGE_STATUS_REFRESHING', False),
                patch('app.routers.system.somar_uso_locais_armazenamento', return_value=(1000, 400, 600)),
                patch('app.routers.system._calcular_status_armazenamento') as calculate,
                patch('app.routers.system.threading.Thread') as thread,
            ):
                response = system_router.obter_status_armazenamento(db)

        engine.dispose()
        calculate.assert_not_called()
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()
        self.assertEqual(response['metricas_status'], 'atualizando')
        self.assertIsNone(response['calculo_previo']['capturas_realizadas'])
        self.assertEqual([item['tipo'] for item in response['provedores']], ['local', 'smb'])
        self.assertEqual(response['provedores'][1]['destino'], r'\\192.168.0.6\capturas')

    def test_camera_media_lists_captures_and_videos_and_rejects_traversal(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with tempfile.TemporaryDirectory() as root, TestSession() as db:
            camera = Camera(nome='Entrada', ip='192.0.2.10', usuario='user', senha='pass')
            db.add(camera)
            db.flush()

            capture_dir = Path(root).resolve() / 'Capturas' / f'camera_{camera.id}' / '01102026'
            capture_dir.mkdir(parents=True)
            (capture_dir / 'frame_20261001_100000.jpg').write_bytes(b'first-frame')
            (capture_dir / 'frame_20261001_110000.jpg').write_bytes(b'last-frame')
            video_dir = Path(root).resolve() / 'Videos' / f'camera_{camera.id}' / '01102026'
            video_dir.mkdir(parents=True)
            video_path = video_dir / 'timelapse_20261001_120000.mp4'
            video_path.write_bytes(b'test-video')

            with (
                patch.object(cameras_router, 'TEMP_DIR', Path(root)),
                patch('app.services.local_storage.TEMP_DIR', Path(root)),
            ):
                media = cameras_router.listar_midias_camera(camera.id, db)
                daily_frames = cameras_router.listar_capturas_data(camera.id, date(2026, 10, 1), db)
                video_response = cameras_router.servir_video_camera(
                    camera.id,
                    0,
                    '01102026/timelapse_20261001_120000.mp4',
                    db,
                )
                with self.assertRaises(HTTPException):
                    cameras_router._camera_media_path(
                        camera.id,
                        'Capturas',
                        0,
                        '../camera_999/frame_20261001_100000.jpg',
                        db,
                    )

        engine.dispose()
        self.assertEqual(media['capturas']['total'], 2)
        self.assertEqual(len(media['capturas']['amostras']), 2)
        self.assertEqual(media['capturas']['dias'], [{'data': '2026-10-01', 'total': 2}])
        self.assertEqual(media['armazenamento']['camera_bytes'], len(b'first-frame') + len(b'last-frame'))
        self.assertGreater(media['armazenamento']['disco_total_bytes'], 0)
        self.assertEqual(daily_frames['data'], '2026-10-01')
        self.assertEqual(len(daily_frames['frames']), 2)
        self.assertEqual(len(media['videos']), 1)
        self.assertEqual(media['videos'][0]['nome'], 'timelapse_20261001_120000.mp4')
        self.assertEqual(Path(video_response.path), video_path)

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
        with (
            patch('app.services.capture_service.escolher_local_de_captura', return_value=None),
            patch('app.services.capture_service.subprocess.run') as ffmpeg,
        ):
            captured = capture_service.capturar_frame_camera(99, 'rtsp://camera/stream', 2)

        self.assertIsNone(captured)
        ffmpeg.assert_not_called()

    def test_capture_storage_selects_active_disk_with_most_free_space(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with tempfile.TemporaryDirectory() as root, TestSession() as db:
            primary = Path(root)
            secondary = primary / 'disk-2'
            secondary.mkdir()
            db.add(LocalStorageLocation(nome='Disco 2', caminho=str(secondary), ativo=True))
            db.commit()

            def fake_mount(path):
                return Path(path).resolve() == secondary.resolve()

            def fake_usage(path):
                if Path(path).resolve() == secondary.resolve():
                    return SimpleNamespace(total=1000, used=100, free=900)
                return SimpleNamespace(total=1000, used=600, free=400)

            with (
                patch.object(local_storage, 'TEMP_DIR', primary),
                patch.object(local_storage, 'SessionLocal', TestSession),
                patch('app.services.local_storage.os.path.ismount', side_effect=fake_mount),
                patch('app.services.local_storage.shutil.disk_usage', side_effect=fake_usage),
            ):
                destination = local_storage.escolher_local_de_captura()

        engine.dispose()
        self.assertEqual(destination, secondary)

    def test_local_cleanup_waits_until_every_active_disk_is_low(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        now = 2_000_000_000
        with tempfile.TemporaryDirectory() as root, TestSession() as db:
            primary = Path(root)
            secondary = primary / 'disk-2'
            capture_dir = primary / 'Capturas' / 'camera_1'
            capture_dir.mkdir(parents=True)
            secondary.mkdir()
            old_capture = capture_dir / 'frame_old.jpg'
            old_capture.write_bytes(b'frame')
            old_mtime = now - 31 * 86400
            os.utime(old_capture, (old_mtime, old_mtime))
            destination = StorageConfig(tipo='smb', ativo=True, host='192.0.2.20', caminho_remoto='captures')
            db.add_all([
                destination,
                LocalStorageLocation(nome='Disco 2', caminho=str(secondary), ativo=True),
            ])
            db.flush()
            stat = old_capture.stat()
            db.add(StorageBackupFile(
                storage_config_id=destination.id,
                local_path='Capturas/camera_1/frame_old.jpg',
                remote_path='backup/frame_old.jpg',
                remote_id='verified-frame',
                file_size=stat.st_size,
                file_mtime_ns=stat.st_mtime_ns,
                backed_up_at=now - 30 * 86400,
                last_error='',
            ))
            db.commit()

            def fake_mount(path):
                return Path(path).resolve() == secondary.resolve()

            def fake_usage(path):
                if Path(path).resolve() == secondary.resolve():
                    return SimpleNamespace(total=1000, used=500, free=500)
                return SimpleNamespace(total=1000, used=960, free=40)

            original_path_stat = Path.stat

            def fake_path_stat(path, *args, **kwargs):
                result = original_path_stat(path, *args, **kwargs)
                if Path(path).resolve() == secondary.resolve():
                    return SimpleNamespace(st_dev=result.st_dev + 1)
                return result

            verifier = Mock(return_value=True)
            with (
                patch.object(local_storage, 'TEMP_DIR', primary),
                patch.object(backup_service, 'ROOT_TEMP', primary),
                patch('app.services.local_storage.os.path.ismount', side_effect=fake_mount),
                patch('app.services.backup_service.shutil.disk_usage', side_effect=fake_usage),
                patch('pathlib.Path.stat', new=fake_path_stat),
            ):
                result = backup_service.limpar_capturas_backupeadas(
                    db,
                    root=primary,
                    min_free_ratio=0.20,
                    now=now,
                    disk_usage=fake_usage,
                    verifier=verifier,
                )

            file_still_exists = old_capture.exists()

        engine.dispose()
        verifier.assert_not_called()
        self.assertTrue(file_still_exists)
        self.assertEqual(result['arquivos_removidos'], 0)
        self.assertFalse(result['precisa_limpeza'])

    def test_local_cleanup_removes_verified_old_file_after_all_disks_are_low(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        now = 2_000_000_000
        with tempfile.TemporaryDirectory() as root, TestSession() as db:
            primary = Path(root)
            secondary = primary / 'disk-2'
            capture_dir = secondary / 'Capturas' / 'camera_1'
            capture_dir.mkdir(parents=True)
            old_capture = capture_dir / 'frame_old.jpg'
            old_capture.write_bytes(b'verified frame')
            old_mtime = now - 31 * 86400
            os.utime(old_capture, (old_mtime, old_mtime))
            destination = StorageConfig(tipo='smb', ativo=True, host='192.0.2.20', caminho_remoto='captures')
            db.add_all([
                destination,
                LocalStorageLocation(nome='Disco 2', caminho=str(secondary), ativo=True),
            ])
            db.flush()
            stat = old_capture.stat()
            db.add(StorageBackupFile(
                storage_config_id=destination.id,
                local_path='local-1/Capturas/camera_1/frame_old.jpg',
                remote_path='backup/local-1/Capturas/camera_1/frame_old.jpg',
                remote_id='verified-frame',
                file_size=stat.st_size,
                file_mtime_ns=stat.st_mtime_ns,
                backed_up_at=now - 30 * 86400,
                last_error='',
            ))
            db.commit()

            def fake_mount(path):
                return Path(path).resolve() == secondary.resolve()

            def fake_usage(path):
                if Path(path).resolve() == secondary.resolve():
                    return SimpleNamespace(total=1000, used=970 if old_capture.exists() else 750, free=30 if old_capture.exists() else 250)
                return SimpleNamespace(total=1000, used=970, free=30)

            original_path_stat = Path.stat

            def fake_path_stat(path, *args, **kwargs):
                result = original_path_stat(path, *args, **kwargs)
                if Path(path).resolve() == secondary.resolve():
                    return SimpleNamespace(st_dev=result.st_dev + 1)
                return result

            verifier = Mock(return_value=True)
            with (
                patch.object(local_storage, 'TEMP_DIR', primary),
                patch.object(backup_service, 'ROOT_TEMP', primary),
                patch('app.services.local_storage.os.path.ismount', side_effect=fake_mount),
                patch('app.services.backup_service.shutil.disk_usage', side_effect=fake_usage),
                patch('pathlib.Path.stat', new=fake_path_stat),
            ):
                result = backup_service.limpar_capturas_backupeadas(
                    db,
                    root=primary,
                    min_free_ratio=0.20,
                    min_age_seconds=0,
                    now=now,
                    disk_usage=fake_usage,
                    verifier=verifier,
                )
                removed = not old_capture.exists()

        engine.dispose()
        verifier.assert_called_once()
        self.assertTrue(removed)
        self.assertEqual(result['arquivos_removidos'], 1)

    def test_local_storage_api_registers_mount_and_remove_keeps_files(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with tempfile.TemporaryDirectory() as root, TestSession() as db:
            primary = Path(root).resolve() / 'primary'
            secondary = primary / 'disk-2'
            secondary.mkdir(parents=True)
            marker = secondary / 'keep.txt'
            marker.write_text('preserve')
            primary.mkdir(exist_ok=True)
            default_device = Path(root).stat().st_dev
            original_path_stat = Path.stat

            def fake_path_stat(path, *args, **kwargs):
                result = original_path_stat(path, *args, **kwargs)
                if Path(path).resolve() == secondary.resolve():
                    return SimpleNamespace(st_dev=default_device + 1)
                return result

            def fake_mount(path):
                return Path(path).resolve() == secondary.resolve()

            with (
                patch.object(local_storage, 'TEMP_DIR', primary),
                patch('app.routers.system.os.path.ismount', side_effect=fake_mount),
                patch('pathlib.Path.stat', new=fake_path_stat),
            ):
                saved = system_router.salvar_destino_local(
                    LocalStorageLocationRequest(nome='Disco secundário', caminho=str(secondary), ativo=True),
                    db,
                )
                listed = system_router.listar_destinos_locais(db)
                removed = system_router.excluir_destino_local(saved['id'], db)
                marker_exists = marker.is_file()

        engine.dispose()
        self.assertEqual(saved['nome'], 'Disco secundário')
        self.assertEqual([location['nome'] for location in listed], ['Principal', 'Disco secundário'])
        self.assertTrue(removed['sucesso'])
        self.assertTrue(marker_exists)

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

    def test_backup_progress_counts_unique_files_and_tracks_each_destination(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        with tempfile.TemporaryDirectory() as directory, TestSession() as db:
            files = []
            for index in range(2):
                path = Path(directory).resolve() / f'frame_{index}.jpg'
                path.write_bytes(b'frame')
                os.utime(path, (1, 1))
                files.append(path)
            smb_config = StorageConfig(tipo='smb', nome='SMB', ativo=True, host='smb.example.test', caminho_remoto='captures')
            drive_config = StorageConfig(tipo='google_drive', nome='Drive', ativo=True, oauth_refresh_token='refresh-token')
            db.add_all([smb_config, drive_config])
            db.commit()
            for path in files:
                stat = path.stat()
                db.add(StorageBackupFile(
                    storage_config_id=smb_config.id,
                    local_path=path.name,
                    remote_path=f'backup/{path.name}',
                    file_size=stat.st_size,
                    file_mtime_ns=stat.st_mtime_ns,
                    backed_up_at=1,
                ))
            first_drive_stat = files[0].stat()
            db.add(StorageBackupFile(
                storage_config_id=drive_config.id,
                local_path=files[0].name,
                remote_path=f'backup/{files[0].name}',
                file_size=first_drive_stat.st_size,
                file_mtime_ns=first_drive_stat.st_mtime_ns,
                backed_up_at=1,
            ))
            db.commit()
            progress_updates = []

            def upload_file(path, config, remote_path):
                return {'id': f'{config.id}-{path.name}', 'size': path.stat().st_size}

            with (
                patch.object(backup_service, '_iter_backup_files', return_value=iter(files)),
                patch.object(backup_service, 'identificar_caminho_local', side_effect=lambda path, _db: path.name),
                patch.object(backup_service, '_remote_backup_path', return_value='backup/frame.jpg'),
                patch.object(backup_service, 'upload_backup_file', side_effect=upload_file),
            ):
                result = backup_service.executar_backup_integral(db, progress_updates.append)

        engine.dispose()
        self.assertTrue(result['sucesso'])
        self.assertEqual(result['enviados'], 1)
        self.assertTrue(progress_updates)
        self.assertTrue(all(update['total'] == 2 for update in progress_updates))
        final = progress_updates[-1]
        self.assertEqual(final['concluidos'], 2)
        self.assertEqual(final['percentual'], 100)
        self.assertEqual(
            [(item['nome'], item['concluidos'], item['total'], item['enviados']) for item in final['destinos']],
            [('Armazenamento SMB', 2, 2, 0), ('Google Drive', 2, 2, 1)],
        )
        smb_finished = next(
            index for index, update in enumerate(progress_updates)
            if update['destino_atual'] == 'Armazenamento SMB'
            and update['destinos'][0]['concluidos'] == 2
        )
        drive_started = next(
            index for index, update in enumerate(progress_updates)
            if update['destino_atual'] == 'Google Drive'
            and update['destinos'][1]['concluidos'] == 0
        )
        self.assertLess(smb_finished, drive_started)

    def test_backup_uploads_files_concurrently_within_each_destination(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        barrier = threading.Barrier(2)
        with tempfile.TemporaryDirectory() as directory, TestSession() as db:
            files = []
            for index in range(2):
                path = Path(directory).resolve() / f'frame_{index}.jpg'
                path.write_bytes(b'frame')
                os.utime(path, (1, 1))
                files.append(path)
            db.add(StorageConfig(tipo='smb', nome='SMB', ativo=True, host='smb.example.test', caminho_remoto='captures'))
            db.commit()

            def upload_file(path, config, remote_path):
                barrier.wait(timeout=5)
                return {'id': path.name, 'size': path.stat().st_size}

            with (
                patch.object(backup_service, '_iter_backup_files', return_value=iter(files)),
                patch.object(backup_service, 'identificar_caminho_local', side_effect=lambda path, _db: path.name),
                patch.object(backup_service, '_remote_backup_path', return_value='backup/frame.jpg'),
                patch.object(backup_service, 'upload_backup_file', side_effect=upload_file),
            ):
                result = backup_service.executar_backup_integral(db)

        engine.dispose()
        self.assertTrue(result['sucesso'])
        self.assertEqual(result['enviados'], 2)

    def test_timelapse_output_is_written_on_destination_filesystem(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            frames = []
            for index in range(10):
                timestamp = f'20260919_{181600 + index:06d}'
                frame = root / f'frame_{timestamp}.jpg'
                frame.write_bytes(b'frame')
                frames.append(str(frame))
            destination_root = root / 'data-disk'
            destination_root.mkdir()
            system_temp = root / 'system-tmp'
            system_temp.mkdir()

            def fake_ffmpeg(command, **kwargs):
                output = Path(command[-1])
                self.assertEqual(output.parent, destination_root / 'Videos' / 'camera_1' / date.today().strftime('%d%m%Y'))
                self.assertIn('-hide_banner', command)
                self.assertIn('-loglevel', command)
                concat_list = Path(command[command.index('-i') + 1])
                concat_lines = concat_list.read_text(encoding='utf-8').splitlines()
                self.assertEqual(sum(line.startswith("file '") for line in concat_lines), 10)
                video_filter = command[command.index('-vf') + 1]
                self.assertEqual(video_filter, 'settb=expr=1/60,setpts=N,scale=-2:1080,fps=60')
                output.write_bytes(b'encoded-video')
                return SimpleNamespace(returncode=0, stderr='')

            with (
                patch.object(video_service, 'SYSTEM_TEMP_DIR', str(system_temp)),
                patch.object(video_service, 'escolher_local_de_captura', return_value=destination_root),
                patch.object(video_service.subprocess, 'run', side_effect=fake_ffmpeg),
            ):
                result = video_service.gerar_timelapse(
                    camera_id=1,
                    data_inicio='2026-09-19T18:16',
                    data_fim='2026-09-19T18:17',
                    fps=60,
                    resolucao='1080p',
                    crf=18,
                    formato='mkv',
                    codec='libx265',
                    caminhos_frames=frames,
                )

            output_path = Path(result['caminho_video'])
            self.assertTrue(result['sucesso'])
            self.assertEqual(output_path.read_bytes(), b'encoded-video')
            self.assertFalse(list(output_path.parent.glob('.timelapse_tmp_*')))

    def test_timelapse_ffmpeg_error_returns_only_last_diagnostic_lines(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            frames = []
            for index in range(10):
                timestamp = f'20260919_{181600 + index:06d}'
                frame = root / f'frame_{timestamp}.jpg'
                frame.write_bytes(b'frame')
                frames.append(str(frame))
            destination_root = root / 'data-disk'
            destination_root.mkdir()
            system_temp = root / 'system-tmp'
            system_temp.mkdir()
            stderr = '\n'.join([f'ffmpeg build line {index}' for index in range(30)] + ['No space left on device'])

            with (
                patch.object(video_service, 'SYSTEM_TEMP_DIR', str(system_temp)),
                patch.object(video_service, 'escolher_local_de_captura', return_value=destination_root),
                patch.object(video_service.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr=stderr)),
            ):
                result = video_service.gerar_timelapse(
                    camera_id=1,
                    data_inicio='2026-09-19T18:16',
                    data_fim='2026-09-19T18:17',
                    fps=60,
                    resolucao='1080p',
                    crf=18,
                    formato='mkv',
                    codec='libx265',
                    caminhos_frames=frames,
                )

        self.assertFalse(result['sucesso'])
        self.assertIn('No space left on device', result['erro'])
        self.assertNotIn('ffmpeg build line 0', result['erro'])

    def test_timelapse_sorts_frames_by_timestamp_not_day_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            october_folder = root / '01102026'
            september_folder = root / '19092026'
            october_folder.mkdir()
            september_folder.mkdir()
            frame_paths = []
            for index in range(5):
                frame = october_folder / f'frame_20261001_00013{index}.jpg'
                frame.write_bytes(b'october-frame')
                frame_paths.append(str(frame))
            for index in range(5):
                frame = september_folder / f'frame_20260919_18160{index}.jpg'
                frame.write_bytes(b'september-frame')
                frame_paths.append(str(frame))
            destination_root = root / 'data-disk'
            destination_root.mkdir()
            system_temp = root / 'system-tmp'
            system_temp.mkdir()
            observed_frames = []

            def fake_ffmpeg(command, **kwargs):
                concat_list = Path(command[command.index('-i') + 1])
                observed_frames.extend(
                    line.removeprefix("file '").removesuffix("'")
                    for line in concat_list.read_text(encoding='utf-8').splitlines()
                    if line.startswith("file '")
                )
                Path(command[-1]).write_bytes(b'encoded-video')
                return SimpleNamespace(returncode=0, stderr='')

            with (
                patch.object(video_service, 'SYSTEM_TEMP_DIR', str(system_temp)),
                patch.object(video_service, 'escolher_local_de_captura', return_value=destination_root),
                patch.object(video_service.subprocess, 'run', side_effect=fake_ffmpeg),
            ):
                result = video_service.gerar_timelapse(
                    camera_id=1,
                    data_inicio='2026-09-19T18:16',
                    data_fim='2026-10-01T00:02',
                    fps=30,
                    resolucao='original',
                    crf=23,
                    formato='mkv',
                    codec='libx264',
                    caminhos_frames=frame_paths,
                )

        self.assertTrue(result['sucesso'])
        timestamps = [Path(path).name.removeprefix('frame_').removesuffix('.jpg') for path in observed_frames]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertTrue(timestamps[0].startswith('20260919_'))
        self.assertTrue(timestamps[-1].startswith('20261001_'))

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
