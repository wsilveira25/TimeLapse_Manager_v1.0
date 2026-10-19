import os
import sys
import time
from apscheduler.schedulers.background import BackgroundScheduler
from app.core.database import SessionLocal
from app.models.camera import Camera
from app.services.alert_service import alertar_falha_captura
from app.services.capture_service import capturar_frame_camera
from app.services.backup_service import executar_backup_agendado
from app.services.update_service import verificar_atualizacao_automatica

scheduler = BackgroundScheduler()

def job_captura_individual(camera_id: int, rtsp_url: str, qualidade: int):
    """Executa a captura isolada para uma única câmara."""
    resultado = capturar_frame_camera(camera_id, rtsp_url, qualidade)
    if resultado is None:
        return

    db = SessionLocal()
    try:
        camera = db.query(Camera).filter(Camera.id == camera_id).first()
        if not camera:
            return
        if resultado:
            if camera.last_capture_alert_at:
                camera.last_capture_alert_at = 0
                db.commit()
        else:
            alertar_falha_captura(db, camera.id, camera.nome or f"Câmera {camera.id}")
    finally:
        db.close()

def executar_reinicio_processo():
    """Reinicia o processo sem depender de escrita no diretório de instalação."""
    time.sleep(2)
    if getattr(sys, "frozen", False):
        os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
    os.execv(sys.executable, [sys.executable, *sys.argv])

def iniciar_agendador():
    """Lê todas as câmaras ativas no arranque e cria os jobs individuais."""
    print("A iniciar o Agendador de Capturas Independente...")
    if not scheduler.running:
        scheduler.start()

    db = SessionLocal()
    try:
        cameras_ativas = db.query(Camera).filter(Camera.ativa == True, Camera.link_rtsp != None).all()

        for cam in cameras_ativas:
            intervalo = getattr(cam, "intervalo_minutos", None)
            if intervalo is None:
                intervalo = getattr(cam, "intervalo_captura", 5)
            qualidade = cam.qualidade_captura if getattr(cam, "qualidade_captura", None) is not None else 2
            job_id = f'captura_cam_{cam.id}'

            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)

            scheduler.add_job(
                job_captura_individual,
                'interval',
                minutes=int(intervalo),
                args=[cam.id, cam.link_rtsp, int(qualidade)],
                id=job_id,
                replace_existing=True
            )
            print(f"[Agendador] Câmara {cam.id} agendada para capturar a cada {intervalo} minuto(s).")
    finally:
        db.close()

    backup_job_id = "backup_integral_5min"
    if scheduler.get_job(backup_job_id):
        scheduler.remove_job(backup_job_id)

    scheduler.add_job(
        executar_backup_agendado,
        "interval",
        minutes=5,
        id=backup_job_id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    print("[Agendador] Backup integral agendado a cada 5 minutos.")

    update_job_id = "atualizacao_automatica"
    scheduler.add_job(
        verificar_atualizacao_automatica,
        "cron",
        hour=3,
        minute=30,
        id=update_job_id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    print("[Agendador] Verificação de atualizações agendada para 03:30.")
