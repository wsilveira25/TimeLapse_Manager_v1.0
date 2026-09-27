// ==========================================
// VARIÁVEIS GLOBAIS
// ==========================================
let perfisConfiguracao = [];
let perfisDescobertos = [];
let camerasCache = [];
let settingsReturnView = null;
const PREFERENCIAS_KEY = 'timelapse.preferences.v1';
const SETTINGS_VIEW_KEY = 'timelapse.settings.active-view';
const CORES_DESTAQUE = new Set(['cyan', 'teal', 'amber', 'coral']);
const FUNDOS_PAINEL = new Set(['deep-blue', 'graphite', 'forest']);
const SETTINGS_VIEWS = new Set(['overview', 'cameras', 'storage', 'alerts', 'users', 'account', 'preferences']);

function ativarViewConfiguracoes(view) {
    if (!SETTINGS_VIEWS.has(view)) return;
    const modal = document.getElementById('modalSettings');
    if (!modal) return;
    modal.querySelectorAll('[data-settings-panel]').forEach((panel) => {
        panel.classList.toggle('active', panel.dataset.settingsPanel === view);
    });
    modal.querySelectorAll('.settings-nav-button').forEach((button) => {
        button.classList.toggle('active', button.dataset.settingsView === view);
    });
    sessionStorage.setItem(SETTINGS_VIEW_KEY, view);
}

function restaurarViewConfiguracoes() {
    if (!settingsReturnView) return;
    const view = settingsReturnView;
    settingsReturnView = null;
    ativarViewConfiguracoes(view);
    bootstrap.Modal.getOrCreateInstance(document.getElementById('modalSettings')).show();
}

function obterPreferencias() {
    try {
        return JSON.parse(localStorage.getItem(PREFERENCIAS_KEY) || '{}');
    } catch {
        return {};
    }
}

function aplicarPreferencias(preferencias = obterPreferencias()) {
    const accent = CORES_DESTAQUE.has(preferencias.accent) ? preferencias.accent : 'cyan';
    const background = FUNDOS_PAINEL.has(preferencias.background) ? preferencias.background : 'deep-blue';
    document.body.dataset.accent = accent;
    document.body.dataset.background = background;
    const backgroundImage = typeof preferencias.backgroundImage === 'string'
        && /^data:image\/(jpeg|png|webp);base64,[a-z0-9+/=]+$/i.test(preferencias.backgroundImage)
        ? preferencias.backgroundImage
        : null;
    if (backgroundImage) {
        document.body.dataset.backgroundImage = 'custom';
        document.body.style.setProperty('--user-background-image', `url("${backgroundImage}")`);
    } else {
        delete document.body.dataset.backgroundImage;
        document.body.style.removeProperty('--user-background-image');
    }
    document.querySelectorAll('[data-accent-choice]').forEach((button) => {
        button.setAttribute('aria-pressed', String(button.dataset.accentChoice === accent));
    });
    const backgroundSelect = document.getElementById('background-choice');
    if (backgroundSelect) backgroundSelect.value = background;
    const preview = document.getElementById('background-image-preview');
    const removeButton = document.getElementById('remove-background-image');
    if (preview) {
        preview.src = backgroundImage || '';
        preview.classList.toggle('d-none', !backgroundImage);
    }
    if (removeButton) removeButton.classList.toggle('d-none', !backgroundImage);
}

function guardarPreferencias(changes) {
    const current = obterPreferencias();
    const next = { ...current, ...changes };
    localStorage.setItem(PREFERENCIAS_KEY, JSON.stringify(next));
    aplicarPreferencias(next);
}

function renderSettingsCameras(cameras) {
    const container = document.getElementById('settings-cameras-list');
    if (!container) return;
    if (!cameras.length) {
        container.innerHTML = '<div class="settings-empty-state">Nenhuma câmera cadastrada.</div>';
        return;
    }
    container.innerHTML = cameras.map((camera) => `
        <article class="settings-list-row">
            <div class="min-w-0">
                <strong>${escaparHtml(camera.nome || `Câmera ${camera.id}`)}</strong>
                <div class="settings-list-meta">${escaparHtml(camera.ip || 'IP não informado')} · ${camera.ativa ? 'Ativa' : 'Inativa'} · intervalo ${Number(camera.intervalo_minutos || 5)} min</div>
            </div>
            <div class="settings-list-actions">
                <button type="button" class="btn btn-sm btn-outline-light" data-camera-settings-id="${Number(camera.id)}"><i class="fa-solid fa-gear me-1"></i>Configurar</button>
            </div>
        </article>
    `).join('');
}

function renderSettingsStorage(providers) {
    const container = document.getElementById('settings-storage-list');
    if (!container) return;
    const storages = (Array.isArray(providers) ? providers : []).filter((item) => item && item.tipo !== 'local');
    if (!storages.length) {
        container.innerHTML = '<div class="settings-empty-state">Nenhum armazenamento remoto cadastrado.</div>';
        return;
    }
    container.innerHTML = storages.map((storage) => `
        <article class="settings-list-row">
            <div class="min-w-0">
                <strong>${escaparHtml(storage.nome || 'Armazenamento')}</strong>
                <div class="settings-list-meta">${escaparHtml(storage.destino || storage.tipo)} · ${storage.ativo ? 'Ativo' : 'Pausado'} · ${Number(storage.capturas_backupeadas || 0).toLocaleString('pt-BR')} capturas confirmadas</div>
            </div>
            <div class="settings-list-actions">
                <button type="button" class="btn btn-sm btn-outline-light" data-settings-storage-edit-id="${Number(storage.id)}"><i class="fa-solid fa-gear me-1"></i>Configurar</button>
            </div>
        </article>
    `).join('');
}

function renderSettingsUsers(users) {
    const container = document.getElementById('settings-users-list');
    if (!container) return;
    if (!Array.isArray(users) || users.length === 0) {
        container.innerHTML = '<div class="settings-empty-state">Nenhum usuário cadastrado.</div>';
        return;
    }
    container.innerHTML = users.map((user) => {
        const isCurrentUser = user.username === document.body.dataset.username;
        return `
        <article class="settings-list-row">
            <div class="min-w-0">
                <strong>${escaparHtml(user.username)}</strong>
                <div class="settings-list-meta">${escaparHtml(user.recovery_email || 'Sem e-mail de recuperação')} · criado ${user.created_at ? new Date(user.created_at * 1000).toLocaleDateString('pt-BR') : '—'}</div>
            </div>
            <div class="settings-list-actions flex-wrap align-items-center">
                ${isCurrentUser
                    ? '<span class="small">Acesso Completo · sua conta</span>'
                    : `<select class="form-select form-select-sm" data-user-role="${Number(user.id)}" aria-label="Perfil de ${escaparHtml(user.username)}">
                        <option value="user" ${user.role === 'user' ? 'selected' : ''}>Comum</option>
                        <option value="admin" ${user.role === 'admin' ? 'selected' : ''}>Acesso Completo</option>
                    </select>
                    <div class="form-check form-switch mb-0"><input class="form-check-input" type="checkbox" data-user-active="${Number(user.id)}" ${user.active ? 'checked' : ''} aria-label="Ativo: ${escaparHtml(user.username)}"></div>
                    <button type="button" class="btn btn-sm btn-outline-light" data-user-save="${Number(user.id)}">Salvar</button>`}
            </div>
        </article>
    `;
    }).join('');
}

async function carregarUsuarios() {
    const response = await fetch('/api/users');
    if (!response.ok) throw new Error('Não foi possível carregar usuários.');
    renderSettingsUsers(await response.json());
}

async function imagemParaPlanoDeFundo(file) {
    if (!file || !file.type.startsWith('image/')) throw new Error('Selecione um arquivo de imagem.');
    if (file.size > 20 * 1024 * 1024) throw new Error('A imagem deve ter no máximo 20 MB.');

    const imageUrl = URL.createObjectURL(file);
    try {
        const image = await new Promise((resolve, reject) => {
            const element = new Image();
            element.onload = () => resolve(element);
            element.onerror = () => reject(new Error('Não foi possível abrir esta imagem.'));
            element.src = imageUrl;
        });
        const scale = Math.min(1, 1920 / image.width, 1200 / image.height);
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(image.width * scale));
        canvas.height = Math.max(1, Math.round(image.height * scale));
        const context = canvas.getContext('2d');
        if (!context) throw new Error('Não foi possível processar esta imagem.');
        context.fillStyle = '#111820';
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.drawImage(image, 0, 0, canvas.width, canvas.height);

        for (const quality of [0.78, 0.62, 0.48]) {
            const dataUrl = canvas.toDataURL('image/jpeg', quality);
            if (dataUrl.length <= 1_800_000) return dataUrl;
        }
        throw new Error('A imagem ainda ficou grande demais após a compressão.');
    } finally {
        URL.revokeObjectURL(imageUrl);
    }
}

// ==========================================
// FUNÇÕES DE ARMAZENAMENTO E STATUS
// ==========================================
function formatarBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return '0 Bytes';

    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(k)), sizes.length - 1);

    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`;
}

function escaparHtml(valor) {
    return String(valor ?? '').replace(/[&<>"']/g, (caractere) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
    })[caractere]);
}

function nomePadraoArmazenamento(tipo) {
    const nomes = {
        smb: 'Armazenamento SMB',
        ftp: 'Armazenamento FTP',
        webdav: 'Armazenamento WebDAV',
        google_drive: 'Google Drive',
        onedrive: 'OneDrive',
    };
    return nomes[tipo] || 'Armazenamento';
}

function renderProvedoresStorage(provedores) {
    const container = document.getElementById('storage-providers');
    if (!container) return;

    const cards = (Array.isArray(provedores) ? provedores : []).filter((item) => item && item.tipo !== 'local').map((item) => {
        const percent = Math.min(Math.max(Number(item.percentagem_uso || 0), 0), 100);
        const oauthProvider = ['google_drive', 'onedrive'].includes(item.tipo);
        const conectado = oauthProvider ? !!item.conectado : !!item.ativo;
        const ativo = oauthProvider && !conectado
            ? 'NÃO CONECTADO'
            : item.ativo ? 'ATIVO' : 'PAUSADO';
        const cor = conectado && item.ativo ? '#19a974' : '#64748b';
        const previsao = item.calculo_previo || {};
        const erro = item.erro ? `
            <div class="alert alert-warning py-2 px-2 mt-3 mb-0 small">${escaparHtml(item.erro)}</div>
        ` : '';
        const calculo = item.erro ? '' : `
            <div class="border-top border-secondary border-opacity-25 mt-3 pt-3">
                <p class="text-uppercase fw-bold text-white mb-2" style="font-size: 0.75rem;">Cálculo Prévio (média: ${Number(previsao.tamanho_medio_kb || 0)} KB)</p>
                <div class="d-flex justify-content-between mb-1 text-light small"><span>Total de capturas:</span><strong>${Number(previsao.capturas_totais || 0).toLocaleString('pt-BR')}</strong></div>
                <div class="d-flex justify-content-between mb-1 text-light small"><span>Capturas realizadas:</span><strong>${Number(previsao.capturas_realizadas || 0).toLocaleString('pt-BR')}</strong></div>
                <div class="d-flex justify-content-between text-light small"><span>Capturas restantes:</span><strong>${Number(previsao.capturas_restantes || 0).toLocaleString('pt-BR')}</strong></div>
            </div>
        `;
        const backupStatus = item.backup_erro
            ? '<div class="alert alert-warning py-2 px-2 mt-3 mb-0 small">Há arquivos pendentes no backup. Eles serão tentados novamente automaticamente.</div>'
            : `<small class="d-block text-light opacity-75 mt-3">Backup integral a cada 5 min · Último sucesso: ${item.backup_ultimo_sucesso ? new Date(item.backup_ultimo_sucesso * 1000).toLocaleString('pt-BR') : 'aguardando primeira cópia'}</small>`;
        const destino = item.destino ? `<small class="d-block text-light opacity-75 text-break">${escaparHtml(item.destino)}</small>` : '';
        return `
            <div class="col-lg-4 col-md-6">
                <div class="card h-100 border-0" style="background: linear-gradient(145deg, #1f2a38, #141b25); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.25);">
                    <div class="card-body p-3 text-white">
                        <div class="d-flex align-items-start justify-content-between gap-2 mb-3">
                            <div class="d-flex align-items-center gap-2 overflow-hidden">
                                <div class="rounded-circle d-flex align-items-center justify-content-center" style="width: 40px; height: 40px; background: rgba(255,255,255,0.08); color: ${cor};">
                                    <i class="fa-solid ${item.tipo === 'google_drive' ? 'fa-google' : 'fa-cloud'}"></i>
                                </div>
                                <div class="overflow-hidden">
                                    <h6 class="mb-0 fw-bold text-white">${escaparHtml(item.nome || 'Armazenamento')}</h6>
                                    <small class="text-light opacity-75">${ativo}</small>
                                    ${destino}
                                </div>
                            </div>
                        </div>
                        <div class="mb-2">
                            <span class="text-light small">Total: ${formatarBytes(item.total_bytes || 0)}</span>
                        </div>
                        <div class="progress" style="height: 24px; background: rgba(0,0,0,0.35); border-radius: 8px;">
                            <div class="progress-bar" role="progressbar" style="width: ${percent}%; background: linear-gradient(90deg, #12d5a5 0%, #00b7ff 100%);">
                                ${percent.toFixed(1)}%
                            </div>
                        </div>
                        <div class="d-flex justify-content-between mt-3 text-light small">
                            <span>Usado: ${formatarBytes(item.usado_bytes || 0)}</span>
                            <span>Livre: ${formatarBytes(item.livre_bytes || 0)}</span>
                        </div>
                        ${erro}
                        ${calculo}
                        ${backupStatus}
                    </div>
                </div>
            </div>
        `;
    }).join('');

    container.innerHTML = cards || `
        <div class="col-12">
            <div class="alert alert-light border mb-0 text-center">Nenhum armazenamento adicional configurado.</div>
        </div>
    `;
}

async function carregarStatusDisco() {
    try {
        const res = await fetch('/api/system/storage');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        const dados = await res.json();
        const disco = dados.disco || {};
        const prev = dados.calculo_previo || {};
        const provedores = Array.isArray(dados.provedores) ? dados.provedores : [];

        const resumo = document.getElementById('disco-resumo');
        if (resumo) {
            resumo.innerText = `Total: ${formatarBytes(disco.total_bytes || 0)} / Livre: ${formatarBytes(disco.livre_bytes || 0)}`;
        }
        const alertaDisco = document.getElementById('disco-alerta');
        if (alertaDisco) {
            if (disco.alerta) {
                alertaDisco.className = `alert alert-${disco.alerta_tipo || 'warning'} py-2 small mt-2 mb-0 text-start`;
                alertaDisco.textContent = disco.alerta;
            } else {
                alertaDisco.classList.add('d-none');
                alertaDisco.textContent = '';
            }
        }

        const barra = document.getElementById('disco-barra');
        if (barra) {
            const percentagem = Number(disco.percentagem_uso || 0);
            barra.style.width = `${percentagem}%`;
            barra.innerText = `${formatarBytes(disco.usado_bytes || 0)} (${percentagem}%)`;

            if (percentagem > 85) {
                barra.style.background = 'linear-gradient(90deg, #ff416c 0%, #ff4b2b 100%)';
                barra.style.boxShadow = '0 0 15px rgba(255, 65, 108, 0.5)';
            }
        }

        const textoMedia = document.getElementById('texto-media-kb');
        if (textoMedia) {
            textoMedia.innerText = `CÁLCULO PRÉVIO (MÉDIA: ${prev.tamanho_medio_kb ?? 0} KB)`;
        }

        const prevTotal = document.getElementById('prev-total');
        if (prevTotal) prevTotal.innerText = Number(prev.capturas_totais || 0).toLocaleString('pt-BR');

        const prevRealizadas = document.getElementById('prev-realizadas');
        if (prevRealizadas) prevRealizadas.innerText = Number(prev.capturas_realizadas || 0).toLocaleString('pt-BR');

        const prevRestantes = document.getElementById('prev-restantes');
        if (prevRestantes) prevRestantes.innerText = Number(prev.capturas_restantes || 0).toLocaleString('pt-BR');

        atualizarProgressoBackup(dados.backup_progresso || null);
        renderProvedoresStorage(provedores);
        renderSettingsStorage(provedores);
    } catch (erro) {
        console.error('Erro ao carregar status do disco:', erro);
        const resumo = document.getElementById('disco-resumo');
        if (resumo) resumo.innerText = 'Erro ao carregar armazenamento.';
    }
}

function atualizarProgressoBackup(status) {
    const container = document.getElementById('backup-progress-container');
    const progressBar = document.getElementById('backup-progress-bar');
    const message = document.getElementById('backup-progress-message');
    const percent = document.getElementById('backup-progress-percent');
    const count = document.getElementById('backup-progress-count');
    if (!container || !progressBar) return;

    status ||= {
        estado: 'aguardando',
        total: 0,
        concluidos: 0,
        enviados: 0,
        percentual: 0,
        mensagem: 'Backup automático agendado a cada 5 minutos.',
    };
    const total = Number(status.total || 0);
    const completed = Number(status.concluidos || 0);
    const value = Math.min(Math.max(Number(status.percentual || 0), 0), 100);
    progressBar.style.width = total === 0 && status.estado === 'executando' ? '12%' : `${value}%`;
    progressBar.setAttribute('aria-valuenow', String(value));
    progressBar.classList.toggle('progress-bar-animated', status.estado === 'executando');
    if (message) message.textContent = status.mensagem || 'Processando backup.';
    if (percent) percent.textContent = total === 0
        ? status.estado === 'executando' ? '...' : '--'
        : `${value}%`;
    if (count) {
        count.textContent = total
            ? `${completed.toLocaleString('pt-BR')} de ${total.toLocaleString('pt-BR')} arquivos · ${Number(status.enviados || 0).toLocaleString('pt-BR')} enviados`
            : status.estado === 'executando' ? 'Preparando lista de arquivos' : 'O status é atualizado automaticamente.';
    }

    const button = document.getElementById('btn-sync-agora');
    if (button) {
        const emExecucao = status.estado === 'executando';
        button.disabled = emExecucao;
        button.innerHTML = emExecucao
            ? '<i class="fa-solid fa-spinner fa-spin me-1"></i>Em andamento'
            : '<i class="fa-solid fa-rotate me-1"></i>Backup agora';
    }
}

async function acompanharBackup(jobId) {
    let status;
    do {
        const response = await fetch(`/api/system/storage/backup/progress/${encodeURIComponent(jobId)}`);
        if (!response.ok) throw new Error('Não foi possível consultar o progresso do backup.');
        status = await response.json();
        atualizarProgressoBackup(status);
        if (status.estado === 'executando') {
            await new Promise((resolve) => window.setTimeout(resolve, 1200));
        }
    } while (status.estado === 'executando');

    await carregarStatusDisco();
    return status;
}

async function carregarConfigNuvem(
    tipo = document.getElementById('nuvem-tipo')?.value,
    configId = null,
    criarNovo = false,
) {
    try {
        const tipoSolicitado = tipo || 'smb';
        const params = new URLSearchParams();
        if (configId) params.set('config_id', configId);
        else if (tipoSolicitado) params.set('tipo', tipoSolicitado);
        const response = criarNovo
            ? null
            : await fetch(`/api/system/storage/config?${params.toString()}`);
        if (response && !response.ok) throw new Error(`HTTP ${response.status}`);

        const dados = response ? await response.json() : { configurado: false };
    const tipoSelecionado = dados.configurado ? dados.tipo : tipoSolicitado;
        const configIdInput = document.getElementById('nuvem-config-id');
        const novoInput = document.getElementById('nuvem-config-novo');
        const nome = document.getElementById('nuvem-nome');
        const tipoSelect = document.getElementById('nuvem-tipo');
        const btnExcluir = document.getElementById('btn-excluir-storage');
        if (tipoSelect) {
            tipoSelect.value = tipoSelecionado;
            tipoSelect.disabled = !!configId;
        }
        if (configIdInput) configIdInput.value = dados.configurado ? dados.id : '';
        if (novoInput) novoInput.value = criarNovo ? '1' : '0';
        if (nome) nome.value = dados.configurado ? dados.nome || nomePadraoArmazenamento(tipoSelecionado) : nomePadraoArmazenamento(tipoSelecionado);
        if (btnExcluir) btnExcluir.style.display = dados.configurado && dados.id ? 'inline-block' : 'none';
        const oauthProvider = ['google_drive', 'onedrive'].includes(tipoSelecionado);
        const host = document.getElementById('nuvem-host');
        const usuario = document.getElementById('nuvem-user');
        const caminho = document.getElementById('nuvem-caminho');
        const ativo = document.getElementById('nuvem-ativa');
        const clientId = document.getElementById('nuvem-client-id');
        const clientSecret = document.getElementById('nuvem-client-secret');
        const camposOauth = document.getElementById('nuvem-campos-oauth');
        const camposLegados = document.getElementById('nuvem-campos-legados');
        const credenciaisLegadas = document.getElementById('nuvem-credenciais-legadas');
        const btnConectar = document.getElementById('btn-conectar-nuvem');
        const callback = document.getElementById('nuvem-oauth-callback');
        const caminhoLabel = document.getElementById('nuvem-caminho-label');
        const caminhoAjuda = document.getElementById('nuvem-caminho-ajuda');

        if (camposOauth) camposOauth.style.display = oauthProvider ? 'block' : 'none';
        if (camposLegados) camposLegados.style.display = oauthProvider ? 'none' : 'block';
        if (credenciaisLegadas) credenciaisLegadas.style.display = oauthProvider ? 'none' : 'flex';
        if (btnConectar) btnConectar.style.display = oauthProvider ? 'inline-block' : 'none';
        if (host) host.value = dados.configurado ? dados.host || '' : '';
        if (usuario) usuario.value = dados.configurado ? dados.usuario || '' : '';
        if (caminho) caminho.value = dados.configurado ? dados.caminho_remoto || '/' : '/';
        if (ativo) ativo.checked = dados.configurado && !!dados.ativo;
        if (clientId) clientId.value = dados.configurado ? dados.oauth_client_id || '' : '';
        if (clientSecret) clientSecret.value = '';
        if (callback) callback.textContent = `${window.location.origin}/api/system/storage/oauth/callback`;
        if (caminhoLabel) caminhoLabel.textContent = tipoSelecionado === 'google_drive'
            ? 'ID da pasta do Google Drive (opcional)'
            : tipoSelecionado === 'onedrive' ? 'Caminho da pasta no OneDrive'
                : tipoSelecionado === 'smb' ? 'Nome da share SMB' : 'Caminho Remoto (Pasta destino)';
        if (caminhoAjuda) caminhoAjuda.textContent = tipoSelecionado === 'google_drive'
            ? 'Deixe / para enviar à raiz; para outra pasta, informe o ID dela.'
            : tipoSelecionado === 'onedrive' ? 'Deixe / para enviar à raiz ou informe o caminho da pasta.'
                : tipoSelecionado === 'smb' ? 'Informe o nome publicado no servidor (ex.: capturas_timelase), sem barras.' : '';

        const titulo = document.getElementById('nuvem-titulo');
        const resumo = document.getElementById('nuvem-desc');
        const btnSync = document.getElementById('btn-sync-agora');
        const iconeCaixa = document.getElementById('icon-nuvem-status');
        const conectado = oauthProvider ? !!dados.conectado : !!dados.host;
        const podeSincronizar = document.body.dataset.admin === 'true' && !!dados.ativo && (!oauthProvider || conectado);

        if (titulo) {
            titulo.innerText = podeSincronizar
                ? `${String(tipoSelecionado).replace('_', ' ').toUpperCase()} ATIVO`
                : conectado ? 'Sincronização Pausada' : oauthProvider ? 'Conta não conectada' : 'Nuvem Não Configurada';
            titulo.className = podeSincronizar ? 'fw-bold text-success mb-1' : 'fw-bold text-warning mb-1';
        }
        if (resumo) {
            resumo.innerText = oauthProvider
                ? conectado ? 'Conta conectada; quota consultada diretamente no provedor.' : 'Salve as credenciais do aplicativo OAuth e conecte sua conta.'
                : dados.ativo ? `Conectado a ${dados.host || 'servidor'}` : 'Configure um servidor TrueNAS ou Nextcloud para backup.';
        }
        if (btnSync) btnSync.style.display = podeSincronizar ? 'block' : 'none';
        if (iconeCaixa) {
            iconeCaixa.style.boxShadow = conectado ? '0 0 15px rgba(25, 135, 84, 0.4)' : '0 0 15px rgba(255, 193, 7, 0.2)';
            iconeCaixa.classList.toggle('bg-success', conectado);
            iconeCaixa.classList.toggle('bg-primary', !conectado);
        }
    } catch (erro) {
        console.error('Erro ao carregar configurações de nuvem:', erro);
    }
}

async function salvarConfigNuvem() {
    const payload = {
        config_id: Number(document.getElementById('nuvem-config-id').value) || null,
        criar_novo: document.getElementById('nuvem-config-novo').value === '1',
        nome: document.getElementById('nuvem-nome').value.trim(),
        tipo: document.getElementById('nuvem-tipo').value,
        host: document.getElementById('nuvem-host').value,
        usuario: document.getElementById('nuvem-user').value,
        senha: document.getElementById('nuvem-pass').value,
        caminho_remoto: document.getElementById('nuvem-caminho').value,
        ativo: document.getElementById('nuvem-ativa').checked,
        oauth_client_id: document.getElementById('nuvem-client-id').value,
        oauth_client_secret: document.getElementById('nuvem-client-secret').value,
    };
    const response = await fetch('/api/system/storage/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const details = await response.json().catch(() => ({}));
        throw new Error(details.detail || 'Erro ao guardar definições.');
    }
    return response.json();
}

async function carregarConfigAlertas() {
    const response = await fetch('/api/system/alerts/config');
    if (!response.ok) throw new Error('Não foi possível carregar as configurações de alerta.');
    const config = await response.json();
    document.getElementById('alerts-smtp-enabled').checked = !!config.smtp_enabled;
    document.getElementById('alerts-smtp-host').value = config.smtp_host || '';
    document.getElementById('alerts-smtp-port').value = Number(config.smtp_port || 587);
    document.getElementById('alerts-smtp-security').value = config.smtp_security || 'starttls';
    document.getElementById('alerts-smtp-username').value = config.smtp_username || '';
    document.getElementById('alerts-smtp-password').value = '';
    document.getElementById('alerts-smtp-password').placeholder = config.smtp_password_configured
        ? 'Deixe em branco para manter a senha atual'
        : 'Informe a senha SMTP';
    document.getElementById('alerts-smtp-sender').value = config.smtp_sender || '';
    document.getElementById('alerts-smtp-recipients').value = config.smtp_recipients || '';
    document.getElementById('alerts-cooldown').value = Number(config.alert_cooldown_minutes || 30);
    document.getElementById('alert-backup-failure').checked = config.alert_backup_failure !== false;
    document.getElementById('alert-camera-failure').checked = config.alert_camera_failure !== false;
    document.getElementById('alert-disk-warning').checked = config.alert_disk_warning !== false;
    document.getElementById('alert-disk-critical').checked = config.alert_disk_critical !== false;
}

async function salvarConfigAlertas(event) {
    event.preventDefault();
    const feedback = document.getElementById('alerts-feedback');
    const payload = {
        smtp_enabled: document.getElementById('alerts-smtp-enabled').checked,
        smtp_host: document.getElementById('alerts-smtp-host').value.trim(),
        smtp_port: Number(document.getElementById('alerts-smtp-port').value),
        smtp_security: document.getElementById('alerts-smtp-security').value,
        smtp_username: document.getElementById('alerts-smtp-username').value.trim(),
        smtp_password: document.getElementById('alerts-smtp-password').value || null,
        clear_smtp_password: document.getElementById('alerts-clear-password').checked,
        smtp_sender: document.getElementById('alerts-smtp-sender').value.trim(),
        smtp_recipients: document.getElementById('alerts-smtp-recipients').value.trim(),
        alert_cooldown_minutes: Number(document.getElementById('alerts-cooldown').value),
        alert_backup_failure: document.getElementById('alert-backup-failure').checked,
        alert_camera_failure: document.getElementById('alert-camera-failure').checked,
        alert_disk_warning: document.getElementById('alert-disk-warning').checked,
        alert_disk_critical: document.getElementById('alert-disk-critical').checked,
    };
    try {
        const response = await fetch('/api/system/alerts/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.detail || 'Não foi possível salvar a configuração SMTP.');
        document.getElementById('alerts-smtp-password').value = '';
        document.getElementById('alerts-clear-password').checked = false;
        if (feedback) {
            feedback.className = 'small mt-3 text-success';
            feedback.textContent = 'Configuração SMTP salva.';
        }
        await carregarConfigAlertas();
    } catch (error) {
        if (feedback) {
            feedback.className = 'small mt-3 text-danger';
            feedback.textContent = error.message || 'Não foi possível salvar a configuração SMTP.';
        }
    }
}

async function testarConfigAlertas() {
    const feedback = document.getElementById('alerts-feedback');
    const button = document.getElementById('alerts-test-email');
    if (button) button.disabled = true;
    if (feedback) {
        feedback.className = 'small mt-3 text-light opacity-75';
        feedback.textContent = 'Enviando e-mail de teste...';
    }
    try {
        const response = await fetch('/api/system/alerts/test', { method: 'POST' });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.detail || 'Não foi possível enviar o e-mail de teste.');
        if (feedback) {
            feedback.className = 'small mt-3 text-success';
            feedback.textContent = 'E-mail de teste enviado.';
        }
    } catch (error) {
        if (feedback) {
            feedback.className = 'small mt-3 text-danger';
            feedback.textContent = error.message || 'Falha no envio do e-mail de teste.';
        }
    } finally {
        if (button) button.disabled = false;
    }
}

// ==========================================
// FUNÇÕES DE CÂMARAS
// ==========================================
async function carregarCameras() {
    const divLista = document.getElementById('lista-cameras');
    if (!divLista) return;

    divLista.innerHTML = '';

    try {
        const response = await fetch('/api/cameras/');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const cameras = await response.json();
        camerasCache = Array.isArray(cameras) ? cameras : [];
        renderSettingsCameras(camerasCache);

        if (camerasCache.length === 0) {
            divLista.innerHTML = `
                <div class="col-12">
                    <div class="alert alert-light border text-center">
                        Nenhuma câmera configurada ainda.
                    </div>
                </div>
            `;
            return;
        }

        camerasCache.forEach((cam) => {
            const statusIcon = cam.ativa ? 'Ativa' : 'Inativa';
            const intervalo = cam.intervalo_minutos ?? cam.intervalo_captura ?? 5;
            const isAtiva = cam.ativa ? 'true' : 'false';
            const isAdmin = document.body.dataset.admin === 'true';
            const cameraActions = isAdmin
                ? `<button type="button" class="btn btn-sm btn-outline-primary" onclick="abrirPreview(${cam.id})">Preview</button>
                   <button type="button" class="btn btn-sm btn-outline-success" onclick="gerarVideo(${cam.id})" title="Gerar vídeo timelapse" aria-label="Gerar vídeo timelapse"><i class="fa-solid fa-film"></i></button>`
                : '<span class="small text-muted">Somente leitura</span>';

            const card = `
                <div class="col-md-6 col-xl-4 mb-4">
                    <div class="card h-100 shadow-sm border-0">
                        <div class="card-body d-flex flex-column">
                            <div class="d-flex justify-content-between align-items-start mb-3">
                                <div>
                                    <h5 class="card-title mb-1">${cam.nome || 'Câmera sem nome'}</h5>
                                    <small class="text-muted">${statusIcon}</small>
                                </div>
                                <span class="badge ${cam.ativa ? 'bg-success' : 'bg-secondary'} rounded-pill">${cam.ativa ? 'ON' : 'OFF'}</span>
                            </div>

                            <ul class="list-unstyled small text-muted mb-3">
                                <li><strong>IP:</strong> ${cam.ip || 'N/D'}</li>
                                <li><strong>Intervalo:</strong> ${intervalo} min</li>
                                <li><strong>Qualidade:</strong> ${cam.qualidade_captura ?? 2}</li>
                            </ul>

                            <div class="mt-auto d-flex gap-2">
                                ${cameraActions}
                            </div>
                        </div>
                    </div>
                </div>
            `;

            divLista.innerHTML += card;
        });
    } catch (erro) {
        console.error('Erro ao carregar câmeras:', erro);
        divLista.innerHTML = `
            <div class="col-12">
                <div class="alert alert-danger text-center">
                    Não foi possível carregar as câmeras.
                </div>
            </div>
        `;
    }
}

function abrirPreview(cameraId) {
    const videoElement = document.getElementById('video-preview');
    if (!videoElement) return;

    videoElement.src = `/api/cameras/${cameraId}/stream`;
    const modal = new bootstrap.Modal(document.getElementById('modalPreview'));
    modal.show();
}

function abrirConfigs(id, intervalo, qualidade, ativa, resolucao_atual) {
    document.getElementById('config-cam-id').value = id;
    document.getElementById('config-intervalo').value = intervalo;
    document.getElementById('config-qualidade').value = qualidade;
    document.getElementById('config-ativa').checked = ativa;

    const selectResolucao = document.getElementById('config-resolucao');
    if (selectResolucao) {
        selectResolucao.innerHTML = '<option value="">Consultando a câmera...</option>';
        selectResolucao.disabled = true;

        fetch(`/api/cameras/${id}/perfis`)
            .then((res) => {
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                return res.json();
            })
            .then((perfis) => {
                perfisConfiguracao = Array.isArray(perfis) ? perfis : [];
                selectResolucao.innerHTML = '<option value="">Selecione uma resolução</option>';
                selectResolucao.disabled = false;

                perfisConfiguracao.forEach((p, index) => {
                    const selected = p.resolucao === resolucao_atual ? 'selected' : '';
                    selectResolucao.innerHTML += `<option value="${index}" ${selected}>${p.nome || 'Perfil'} - ${p.resolucao || 'Desconhecida'} (${p.codec || 'N/D'})</option>`;
                });
            })
            .catch(() => {
                selectResolucao.innerHTML = '<option value="">Falha ao ler câmera.</option>';
            });
    }

    const modal = new bootstrap.Modal(document.getElementById('modalConfigs'));
    modal.show();
}

function frameParaDataLocal(valor, adicionarMinuto = false) {
    if (!valor) return '';
    const [data, hora] = valor.split('_');
    if (!data || !hora) return '';
    const dataLocal = new Date(
        Number(data.slice(0, 4)),
        Number(data.slice(4, 6)) - 1,
        Number(data.slice(6, 8)),
        Number(hora.slice(0, 2)),
        Number(hora.slice(2, 4)),
    );
    if (adicionarMinuto) dataLocal.setMinutes(dataLocal.getMinutes() + 1);
    const pad = (number) => String(number).padStart(2, '0');
    return `${dataLocal.getFullYear()}-${pad(dataLocal.getMonth() + 1)}-${pad(dataLocal.getDate())}T${pad(dataLocal.getHours())}:${pad(dataLocal.getMinutes())}`;
}

async function carregarDestinosBackup() {
    const select = document.getElementById('gerador-backup-storage');
    if (!select || select.dataset.loaded === 'true') return;
    const response = await fetch('/api/system/storage/backup/sources');
    if (!response.ok) throw new Error('Não foi possível listar os destinos de backup.');
    const destinos = await response.json();
    const disponiveis = destinos.filter((destino) => destino.conectado);
    select.innerHTML = disponiveis.length
        ? disponiveis.map((destino) => `<option value="${Number(destino.id)}">${escaparHtml(destino.nome)} (${escaparHtml(destino.tipo)})</option>`).join('')
        : '<option value="">Nenhum destino ativo/conectado</option>';
    select.dataset.loaded = 'true';
}

async function carregarInfoFramesVideo(cameraId) {
    const infoTotal = document.getElementById('info-total-frames');
    const infoDatas = document.getElementById('info-datas-frames');
    const origem = document.getElementById('gerador-origem')?.value || 'local';
    const storageId = document.getElementById('gerador-backup-storage')?.value;
    if (infoTotal) infoTotal.innerText = 'Consultando...';
    if (infoDatas) infoDatas.innerText = '';

    if (origem === 'backup' && !storageId) {
        if (infoTotal) infoTotal.innerText = '0';
        if (infoDatas) infoDatas.innerText = 'Nenhum destino de backup ativo e conectado.';
        return;
    }

    try {
        const query = origem === 'backup' && storageId ? `?storage_config_id=${encodeURIComponent(storageId)}` : '';
        const response = await fetch(`/api/cameras/${cameraId}/frames-info${query}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (infoTotal) infoTotal.innerText = Number(data.total || 0).toLocaleString('pt-BR');

        if (data.total > 0) {
            if (infoDatas) infoDatas.innerText = `Desde: ${data.primeiro} | Até: ${data.ultimo}`;
            document.getElementById('gerador-inicio').value = frameParaDataLocal(data.primeiro);
            document.getElementById('gerador-fim').value = frameParaDataLocal(data.ultimo, true);
        } else if (infoDatas) {
            infoDatas.innerText = origem === 'backup'
                ? 'Ainda não há frames sincronizados neste destino para esta câmera.'
                : 'Ainda não existem fotos capturadas para gerar vídeo.';
        }
    } catch (error) {
        console.error('Erro ao consultar frames:', error);
        if (infoTotal) infoTotal.innerText = 'Falha na consulta';
        if (infoDatas) infoDatas.innerText = error.message;
    }
}

async function gerarVideo(cameraId) {
    document.getElementById('gerador-cam-id').value = cameraId;
    const modal = new bootstrap.Modal(document.getElementById('modalGeradorVideo'));
    const origem = document.getElementById('gerador-origem');
    const backupWrap = document.getElementById('gerador-backup-storage-wrap');
    try {
        if (origem) origem.value = 'local';
        if (backupWrap) backupWrap.style.display = 'none';
        await carregarInfoFramesVideo(cameraId);
    } catch (e) {
        console.error('Erro ao preparar o gerador:', e);
    }

    modal.show();
}

async function pedirReinicio(modo) {
    try {
        const res = await fetch(`/api/system/restart?modo=${modo}`, { method: 'POST' });
        const dados = await res.json();

        if (dados.sucesso) {
            const reiniciarModal = bootstrap.Modal.getInstance(document.getElementById('modalReiniciar'));
            if (reiniciarModal) reiniciarModal.hide();

            if (modo === 'agora') {
                alert('A reiniciar o sistema... A página será recarregada em 5 segundos.');
                setTimeout(() => location.reload(), 5000);
            } else {
                alert(dados.mensagem);
            }
        } else {
            alert('Falha: ' + dados.mensagem);
        }
    } catch (e) {
        console.error('Erro ao solicitar reinício:', e);
    }
}

// ==========================================
// EVENTOS DE INICIALIZAÇÃO DA PÁGINA
// ==========================================
document.addEventListener('DOMContentLoaded', async () => {
    aplicarPreferencias();

    const settingsModalElement = document.getElementById('modalSettings');
    if (settingsModalElement) {
        settingsModalElement.addEventListener('click', (event) => {
            const viewButton = event.target.closest('[data-settings-view]');
            if (viewButton) {
                ativarViewConfiguracoes(viewButton.dataset.settingsView);
                if (viewButton.dataset.settingsView === 'users') {
                    carregarUsuarios().catch((error) => {
                        const feedback = document.getElementById('users-feedback');
                        if (feedback) feedback.textContent = error.message;
                    });
                }
                return;
            }

            const accentButton = event.target.closest('[data-accent-choice]');
            if (accentButton) {
                guardarPreferencias({ accent: accentButton.dataset.accentChoice });
                return;
            }

            const actionButton = event.target.closest('[data-settings-action]');
            if (actionButton) {
                settingsModalElement.dataset.pendingAction = actionButton.dataset.settingsAction;
                bootstrap.Modal.getOrCreateInstance(settingsModalElement).hide();
            }
        });

        settingsModalElement.addEventListener('hidden.bs.modal', () => {
            const action = settingsModalElement.dataset.pendingAction;
            const cameraId = Number(settingsModalElement.dataset.pendingCameraId || 0);
            const storageId = Number(settingsModalElement.dataset.pendingStorageId || 0);
            const deveVoltar = cameraId || storageId || action === 'camera' || action === 'storage';
            settingsReturnView = deveVoltar ? sessionStorage.getItem(SETTINGS_VIEW_KEY) || 'overview' : null;
            if (!deveVoltar) sessionStorage.removeItem(SETTINGS_VIEW_KEY);
            delete settingsModalElement.dataset.pendingAction;
            delete settingsModalElement.dataset.pendingCameraId;
            delete settingsModalElement.dataset.pendingStorageId;
            if (cameraId) {
                const camera = camerasCache.find((item) => item.id === cameraId);
                if (camera) abrirConfigs(camera.id, Number(camera.intervalo_minutos || 5), Number(camera.qualidade_captura || 2), Boolean(camera.ativa), camera.resolucao || '');
            } else if (storageId) {
                carregarConfigNuvem(undefined, storageId).then(() => {
                    bootstrap.Modal.getOrCreateInstance(document.getElementById('modalConfigNuvem')).show();
                });
            } else if (action === 'camera') {
                bootstrap.Modal.getOrCreateInstance(document.getElementById('modalAdicionarCamera')).show();
            } else if (action === 'storage') {
                abrirNovoStorage();
            } else if (action === 'backup') {
                document.getElementById('btn-sync-agora')?.click();
            }
        });

        settingsModalElement.addEventListener('hidden.bs.modal', () => {
            if (!settingsModalElement.dataset.pendingAction
                && !settingsModalElement.dataset.pendingCameraId
                && !settingsModalElement.dataset.pendingStorageId
                && !settingsReturnView) {
                sessionStorage.removeItem(SETTINGS_VIEW_KEY);
            }
        });
    }

    for (const modalId of ['modalAdicionarCamera', 'modalConfigNuvem']) {
        document.getElementById(modalId)?.addEventListener('hidden.bs.modal', restaurarViewConfiguracoes);
    }
    document.getElementById('modalConfigs')?.addEventListener('hidden.bs.modal', () => {
        if (!document.getElementById('modalReiniciar')?.classList.contains('show')) {
            restaurarViewConfiguracoes();
        }
    });
    document.getElementById('modalReiniciar')?.addEventListener('hidden.bs.modal', restaurarViewConfiguracoes);

    document.getElementById('background-choice')?.addEventListener('change', (event) => {
        guardarPreferencias({ background: event.target.value });
    });

    document.getElementById('reset-preferences')?.addEventListener('click', () => {
        localStorage.removeItem(PREFERENCIAS_KEY);
        aplicarPreferencias({});
    });

    const backgroundImageInput = document.getElementById('background-image-file');
    document.getElementById('choose-background-image')?.addEventListener('click', () => backgroundImageInput?.click());
    backgroundImageInput?.addEventListener('change', async () => {
        const file = backgroundImageInput.files?.[0];
        if (!file) return;
        try {
            const backgroundImage = await imagemParaPlanoDeFundo(file);
            guardarPreferencias({ backgroundImage });
        } catch (error) {
            alert(error.message || 'Não foi possível aplicar esta imagem.');
        } finally {
            backgroundImageInput.value = '';
        }
    });
    document.getElementById('remove-background-image')?.addEventListener('click', () => {
        guardarPreferencias({ backgroundImage: null });
    });

    document.getElementById('alert-settings-form')?.addEventListener('submit', salvarConfigAlertas);
    document.getElementById('alerts-test-email')?.addEventListener('click', testarConfigAlertas);

    carregarConfigAlertas().catch((error) => {
        console.error('Erro ao carregar configuração SMTP:', error);
        const feedback = document.getElementById('alerts-feedback');
        if (feedback) {
            feedback.className = 'small mt-3 text-danger';
            feedback.textContent = 'Não foi possível carregar as configurações de alerta.';
        }
    });

    const userCreateForm = document.getElementById('user-create-form');
    userCreateForm?.addEventListener('submit', async (event) => {
        event.preventDefault();
        const feedback = document.getElementById('users-feedback');
        const payload = {
            username: document.getElementById('new-user-name').value.trim(),
            recovery_email: document.getElementById('new-user-email').value.trim(),
            password: document.getElementById('new-user-password').value,
            role: document.getElementById('new-user-role').value,
        };
        try {
            const response = await fetch('/api/users', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const result = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(result.detail || 'Não foi possível criar usuário.');
            userCreateForm.reset();
            if (feedback) {
                feedback.className = 'small mt-2 text-success';
                feedback.textContent = 'Usuário criado.';
            }
            await carregarUsuarios();
        } catch (error) {
            if (feedback) {
                feedback.className = 'small mt-2 text-danger';
                feedback.textContent = error.message || 'Não foi possível criar usuário.';
            }
        }
    });

    document.getElementById('settings-users-list')?.addEventListener('click', async (event) => {
        const button = event.target.closest('[data-user-save]');
        if (!button) return;
        const userId = Number(button.dataset.userSave);
        const role = document.querySelector(`[data-user-role="${userId}"]`)?.value;
        const active = document.querySelector(`[data-user-active="${userId}"]`)?.checked;
        const feedback = document.getElementById('users-feedback');
        try {
            const response = await fetch(`/api/users/${userId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ role, active }),
            });
            const result = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(result.detail || 'Não foi possível atualizar usuário.');
            if (feedback) {
                feedback.className = 'small mt-2 text-success';
                feedback.textContent = 'Usuário atualizado.';
            }
            await carregarUsuarios();
        } catch (error) {
            if (feedback) {
                feedback.className = 'small mt-2 text-danger';
                feedback.textContent = error.message || 'Não foi possível atualizar usuário.';
            }
        }
    });

    document.getElementById('recovery-email-form')?.addEventListener('submit', async (event) => {
        event.preventDefault();
        const feedback = document.getElementById('account-email-feedback');
        const body = new URLSearchParams({ email: document.getElementById('account-recovery-email').value.trim() });
        try {
            const response = await fetch('/api/auth/recovery-email', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body });
            const result = await response.json();
            if (!response.ok) throw new Error(result.detail || 'Não foi possível salvar o e-mail.');
            if (feedback) {
                feedback.className = 'small mt-2 text-success';
                feedback.textContent = result.mensagem;
            }
        } catch (error) {
            if (feedback) {
                feedback.className = 'small mt-2 text-danger';
                feedback.textContent = error.message || 'Não foi possível salvar o e-mail.';
            }
        }
    });

    document.getElementById('change-password-form')?.addEventListener('submit', async (event) => {
        event.preventDefault();
        const feedback = document.getElementById('change-password-feedback');
        const body = new URLSearchParams({
            current_password: document.getElementById('current-password').value,
            password: document.getElementById('new-password').value,
            password_confirm: document.getElementById('confirm-password').value,
        });
        try {
            const response = await fetch('/api/auth/password/change', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body });
            const result = await response.json();
            if (!response.ok) throw new Error(result.detail || 'Não foi possível alterar a senha.');
            document.getElementById('change-password-form').reset();
            if (feedback) {
                feedback.className = 'small mt-2 text-success';
                feedback.textContent = result.mensagem;
            }
        } catch (error) {
            if (feedback) {
                feedback.className = 'small mt-2 text-danger';
                feedback.textContent = error.message || 'Não foi possível alterar a senha.';
            }
        }
    });

    const oauthResult = new URLSearchParams(window.location.search).get('storage_oauth');
    if (oauthResult) {
        const oauthMessages = {
            connected: 'Conta cloud conectada com sucesso.',
            quota_warning: 'Conta conectada, mas não foi possível consultar a quota agora. O painel tentará novamente automaticamente.',
            denied: 'A autorização cloud foi cancelada.',
            error: 'Não foi possível concluir a autorização cloud. Verifique as credenciais e os logs do servidor.',
        };
        alert(oauthMessages[oauthResult] || 'Resultado inesperado da autorização cloud.');
        window.history.replaceState({}, document.title, window.location.pathname);
    }

    const isAdmin = document.body.dataset.admin === 'true';
    if (isAdmin) {
        await Promise.all([
            carregarCameras(),
            carregarStatusDisco(),
            carregarConfigNuvem(),
        ]);
    }

    const overlay = document.getElementById('loading-overlay');
    if (overlay) {
        overlay.style.opacity = '0';
        setTimeout(() => overlay.remove(), 400);
    }

    const savedSettingsView = sessionStorage.getItem(SETTINGS_VIEW_KEY);
    if (SETTINGS_VIEWS.has(savedSettingsView)) {
        ativarViewConfiguracoes(savedSettingsView);
        bootstrap.Modal.getOrCreateInstance(document.getElementById('modalSettings')).show();
    }

    if (isAdmin) setInterval(carregarStatusDisco, 5000);

    const tipoNuvem = document.getElementById('nuvem-tipo');
    if (tipoNuvem) {
        tipoNuvem.addEventListener('change', () => {
            const criarNovo = document.getElementById('nuvem-config-novo')?.value === '1';
            carregarConfigNuvem(tipoNuvem.value, null, criarNovo);
        });
    }

    const abrirNovoStorage = async () => {
        await carregarConfigNuvem('smb', null, true);
        bootstrap.Modal.getOrCreateInstance(document.getElementById('modalConfigNuvem')).show();
    };

    const storageCards = document.getElementById('storage-providers');
    if (storageCards) {
        storageCards.addEventListener('click', async (event) => {
            const button = event.target.closest('[data-storage-edit-id]');
            if (!button) return;
            await carregarConfigNuvem(undefined, button.dataset.storageEditId);
            bootstrap.Modal.getOrCreateInstance(document.getElementById('modalConfigNuvem')).show();
        });
    }

    const settingsStorageList = document.getElementById('settings-storage-list');
    if (settingsStorageList) {
        settingsStorageList.addEventListener('click', async (event) => {
            const button = event.target.closest('[data-settings-storage-edit-id]');
            if (!button) return;
            settingsModalElement.dataset.pendingStorageId = button.dataset.settingsStorageEditId;
            bootstrap.Modal.getOrCreateInstance(settingsModalElement).hide();
        });
    }

    const settingsCamerasList = document.getElementById('settings-cameras-list');
    if (settingsCamerasList) {
        settingsCamerasList.addEventListener('click', (event) => {
            const button = event.target.closest('[data-camera-settings-id]');
            if (!button) return;
            settingsModalElement.dataset.pendingCameraId = button.dataset.cameraSettingsId;
            bootstrap.Modal.getOrCreateInstance(settingsModalElement).hide();
        });
    }

    const origemVideo = document.getElementById('gerador-origem');
    const storageBackupSelect = document.getElementById('gerador-backup-storage');
    const storageBackupWrap = document.getElementById('gerador-backup-storage-wrap');
    if (origemVideo) {
        origemVideo.addEventListener('change', async () => {
            const usarBackup = origemVideo.value === 'backup';
            if (storageBackupWrap) storageBackupWrap.style.display = usarBackup ? 'block' : 'none';
            if (usarBackup) {
                try {
                    await carregarDestinosBackup();
                } catch (error) {
                    console.error('Erro ao carregar destinos de backup:', error);
                }
            }
            const cameraId = document.getElementById('gerador-cam-id').value;
            if (cameraId) await carregarInfoFramesVideo(cameraId);
        });
    }
    if (storageBackupSelect) {
        storageBackupSelect.addEventListener('change', () => {
            const cameraId = document.getElementById('gerador-cam-id').value;
            if (cameraId) carregarInfoFramesVideo(cameraId);
        });
    }

    const btnDescobrir = document.getElementById('btn-descobrir');
    if (btnDescobrir) {
        btnDescobrir.addEventListener('click', async function () {
            const btn = this;

            if (btn.innerText.includes('Conectar')) {
                btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Conectando...';
                btn.disabled = true;

                const ip = document.getElementById('cam-ip').value;
                const porta = parseInt(document.getElementById('cam-porta').value, 10);
                const usar_https = document.getElementById('cam-https').checked;
                const usuario = document.getElementById('cam-user').value;
                const senha = document.getElementById('cam-pass').value;

                try {
                    const res = await fetch('/api/cameras/descobrir', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ ip, porta, usuario, senha, usar_https }),
                    });
                    const dados = await res.json();

                    if (!res.ok) {
                        alert('Erro: ' + (dados.detail || 'Falha ao descobrir câmeras.'));
                        btn.innerHTML = 'Conectar e Descobrir';
                        btn.disabled = false;
                        return;
                    }

                    perfisDescobertos = Array.isArray(dados.perfis) ? dados.perfis : [];
                    const select = document.getElementById('cam-perfil-selecionado');
                    if (select) {
                        select.innerHTML = perfisDescobertos
                            .map((p, index) => `<option value="${index}">${p.nome || 'Perfil'} - ${p.resolucao || 'Desconhecida'} (${p.codec || 'N/D'})</option>`)
                            .join('');
                    }

                    const passo2 = document.getElementById('passo-2');
                    if (passo2) passo2.style.display = 'block';
                    btn.innerHTML = '<i class="fa-solid fa-floppy-disk me-2"></i>Salvar Câmera';
                    btn.classList.replace('btn-success', 'btn-primary');
                    btn.disabled = false;
                } catch (erro) {
                    console.error('Erro de comunicação com a câmera:', erro);
                    alert('Erro de comunicação com a câmera.');
                    btn.innerHTML = 'Conectar e Descobrir';
                    btn.disabled = false;
                }
            } else {
                btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Salvando...';
                btn.disabled = true;

                const nome = document.getElementById('cam-nome').value;
                const ip = document.getElementById('cam-ip').value;
                const porta = parseInt(document.getElementById('cam-porta').value, 10);
                const usar_https = document.getElementById('cam-https').checked;
                const usuario = document.getElementById('cam-user').value;
                const senha = document.getElementById('cam-pass').value;

                const indexSelecionado = document.getElementById('cam-perfil-selecionado').value;
                const perfil = perfisDescobertos[Number(indexSelecionado)];

                if (!perfil) {
                    alert('Selecione um perfil válido antes de guardar.');
                    btn.innerHTML = 'Conectar e Descobrir';
                    btn.disabled = false;
                    return;
                }

                const resSalvar = await fetch('/api/cameras/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        nome,
                        ip,
                        porta,
                        usar_https,
                        usuario,
                        senha,
                        resolucao: perfil.resolucao,
                        link_rtsp: perfil.rtsp_url,
                    }),
                });

                if (resSalvar.ok) {
                    alert('Câmera salva com sucesso!');
                   bootstrap.Modal.getInstance(document.getElementById('modalAdicionarCamera')).hide();
                    document.getElementById('form-descoberta').reset();
                    document.getElementById('passo-2').style.display = 'none';
                    btn.innerHTML = 'Conectar e Descobrir';
                    btn.classList.replace('btn-primary', 'btn-success');
                    btn.disabled = false;
                    carregarCameras();
                } else {
                    alert('Erro ao salvar.');
                    btn.innerHTML = 'Conectar e Descobrir';
                    btn.disabled = false;
                }
            }
        });
    }

    const modalPreviewEl = document.getElementById('modalPreview');
    if (modalPreviewEl) {
        modalPreviewEl.addEventListener('hidden.bs.modal', function () {
            const preview = document.getElementById('video-preview');
            if (preview) preview.src = '';
        });
    }

    const btnDeletarCam = document.getElementById('btn-deletar-cam');
    if (btnDeletarCam) {
        btnDeletarCam.addEventListener('click', async function () {
            const id = document.getElementById('config-cam-id').value;
            if (confirm('Tem certeza que deseja excluir esta câmera permanentemente?')) {
                try {
                    const res = await fetch(`/api/cameras/${id}`, { method: 'DELETE' });
                    if (res.ok) {
                        bootstrap.Modal.getInstance(document.getElementById('modalConfigs')).hide();
                        carregarCameras();
                    } else {
                        alert('Erro ao excluir');
                    }
                } catch (e) {
                    alert('Erro de comunicação.');
                }
            }
        });
    }

    const btnSalvarConfigs = document.getElementById('btn-salvar-configs');
    if (btnSalvarConfigs) {
        btnSalvarConfigs.addEventListener('click', async function () {
            const id = document.getElementById('config-cam-id').value;
            const intervalo_minutos = parseInt(document.getElementById('config-intervalo').value, 10);
            const qualidade_captura = parseInt(document.getElementById('config-qualidade').value, 10);
            const ativa = document.getElementById('config-ativa').checked;

            const indexResolucao = document.getElementById('config-resolucao').value;
            let resolucao = null;
            let link_rtsp = null;

            if (indexResolucao !== '' && perfisConfiguracao[Number(indexResolucao)]) {
                resolucao = perfisConfiguracao[Number(indexResolucao)].resolucao;
                link_rtsp = perfisConfiguracao[Number(indexResolucao)].rtsp_url;
            }

            try {
                const res = await fetch(`/api/cameras/${id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ intervalo_minutos, ativa, qualidade_captura, resolucao, link_rtsp }),
                });

                if (res.ok) {
                    bootstrap.Modal.getInstance(document.getElementById('modalConfigs')).hide();
                    new bootstrap.Modal(document.getElementById('modalReiniciar')).show();
                    carregarCameras();
                } else {
                    alert('Erro ao salvar configurações');
                }
            } catch (e) {
                alert('Erro de comunicação com o servidor.');
            }
        });
    }

    const btnSalvarNuvem = document.getElementById('btn-salvar-nuvem');
    if (btnSalvarNuvem) {
        btnSalvarNuvem.addEventListener('click', async function () {
            try {
                const saved = await salvarConfigNuvem();
                document.getElementById('nuvem-config-id').value = saved.id;
                document.getElementById('nuvem-config-novo').value = '0';
                bootstrap.Modal.getInstance(document.getElementById('modalConfigNuvem')).hide();
                await Promise.all([
                    carregarConfigNuvem(document.getElementById('nuvem-tipo').value, saved.id),
                    carregarStatusDisco(),
                ]);
            } catch (e) {
                alert(e.message || 'Falha na comunicação.');
            }
        });
    }

    const btnExcluirStorage = document.getElementById('btn-excluir-storage');
    if (btnExcluirStorage) {
        btnExcluirStorage.addEventListener('click', async () => {
            const configId = document.getElementById('nuvem-config-id').value;
            if (!configId || !window.confirm('Excluir esta configuração de armazenamento?')) return;
            try {
                const response = await fetch(`/api/system/storage/config/${configId}`, { method: 'DELETE' });
                if (!response.ok) throw new Error('Não foi possível excluir o armazenamento.');
                bootstrap.Modal.getInstance(document.getElementById('modalConfigNuvem')).hide();
                await Promise.all([carregarStatusDisco(), carregarConfigNuvem('smb')]);
            } catch (error) {
                alert(error.message || 'Falha ao excluir armazenamento.');
            }
        });
    }

    const btnConectarNuvem = document.getElementById('btn-conectar-nuvem');
    if (btnConectarNuvem) {
        btnConectarNuvem.addEventListener('click', async function () {
            const tipo = document.getElementById('nuvem-tipo').value;
            try {
                const saved = await salvarConfigNuvem();
                window.location.href = `/api/system/storage/oauth/${encodeURIComponent(tipo)}/start?config_id=${saved.id}`;
            } catch (e) {
                alert(e.message || 'Não foi possível salvar a configuração OAuth.');
            }
        });
    }

    const btnSyncAgora = document.getElementById('btn-sync-agora');
    if (btnSyncAgora) {
        btnSyncAgora.addEventListener('click', async function () {
            const btn = this;
            const originalText = btn.innerHTML;
            atualizarProgressoBackup({ estado: 'executando', mensagem: 'Iniciando backup.' });
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Sincronizando...';
            btn.disabled = true;

            try {
                const res = await fetch('/api/system/storage/backup', { method: 'POST' });
                if (!res.ok) throw new Error('Não foi possível iniciar o backup.');
                const { job_id: jobId } = await res.json();
                const resultado = await acompanharBackup(jobId);
                atualizarProgressoBackup(resultado);
            } catch {
                const message = document.getElementById('backup-progress-message');
                if (message) message.textContent = 'Não foi possível acompanhar o backup. Tente novamente em instantes.';
            } finally {
                btn.innerHTML = originalText;
                btn.disabled = false;
            }
        });
    }

    const btnIniciarRender = document.getElementById('btn-iniciar-render');
    if (btnIniciarRender) {
        btnIniciarRender.addEventListener('click', async function () {
            const btn = this;
            const cameraId = document.getElementById('gerador-cam-id').value;
            const data_inicio = document.getElementById('gerador-inicio').value;
            const data_fim = document.getElementById('gerador-fim').value;
            const fps = parseInt(document.getElementById('gerador-fps').value, 10);
            const resolucao = document.getElementById('gerador-resolucao').value;
            const crf = parseInt(document.getElementById('gerador-crf').value, 10);
            const formato = document.getElementById('gerador-formato').value;
            const codec = document.getElementById('gerador-codec').value;
            const origem = document.getElementById('gerador-origem').value;
            const storageConfigId = document.getElementById('gerador-backup-storage').value;

            if (!data_inicio || !data_fim) {
                alert('Por favor, selecione as datas de início e fim.');
                return;
            }
            if (origem === 'backup' && !storageConfigId) {
                alert('Selecione um destino de backup ativo e conectado.');
                return;
            }

            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>A renderizar... (Pode demorar)';
            btn.disabled = true;

            try {
                const res = await fetch(`/api/cameras/${cameraId}/gerar-video`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        data_inicio,
                        data_fim,
                        fps,
                        resolucao,
                        crf,
                        formato,
                        codec,
                        storage_config_id: origem === 'backup' ? Number(storageConfigId) : null,
                    }),
                });
                const dados = await res.json();

                if (res.ok) {
                    alert(`Sucesso!\nForam compiladas ${dados.total_frames || 0} fotos.\nGuardado em: ${dados.caminho_video || 'local'}`);
                    bootstrap.Modal.getInstance(document.getElementById('modalGeradorVideo')).hide();
                } else {
                    alert('Erro ao renderizar vídeo: ' + (dados.detail || 'Falha desconhecida'));
                }
            } catch (erro) {
                alert('Falha na comunicação com o servidor.');
            } finally {
                btn.innerHTML = '<i class="fa-solid fa-play me-2"></i>Iniciar Renderização';
                btn.disabled = false;
            }
        });
    }
});
