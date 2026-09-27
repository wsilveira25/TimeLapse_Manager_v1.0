from sqlalchemy import Column, Integer, String, Boolean, DateTime
from datetime import datetime
from app.core.database import Base

class Camera(Base):
    __tablename__ = "cameras"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, index=True)
    ip = Column(String, nullable=False)
    porta = Column(Integer, default=80)
    usar_https = Column(Boolean, default=False)
    usuario = Column(String, nullable=False)
    senha = Column(String, nullable=False)
    resolucao = Column(String, nullable=True)
    link_rtsp = Column(String, nullable=True)
    
    # NOVAS CONFIGURAÇÕES
    intervalo_minutos = Column(Integer, default=5)
    ativa = Column(Boolean, default=True)
    qualidade_captura = Column(Integer, default=2)    
    last_capture_alert_at = Column(Integer, nullable=False, default=0)
    criada_em = Column(DateTime, default=datetime.utcnow)
