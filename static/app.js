// Todo el JavaScript de la aplicación vive aquí, y ninguna plantilla lleva un
// <script> en línea ni un onclick. No es una cuestión de orden: es lo que
// permite servir una Content-Security-Policy con "script-src 'self'" sin
// 'unsafe-inline'. Con 'unsafe-inline' la CSP no frena un XSS —que es lo único
// para lo que sirve—, porque el script inyectado se ejecutaría igual.
//
// El precio es que los datos que antes iban como argumentos de la llamada
// (openPreview(3, 'image/png', ...)) ahora viajan en atributos data-* y se leen
// desde aquí.
(function () {
  'use strict';

  // ── Modales ────────────────────────────────────────────────────────────────
  function closeModal(id) {
    const overlay = document.getElementById(id);
    if (!overlay) return;
    overlay.classList.remove('open');
    // El cuerpo de la vista previa se vacía al cerrar: si no, el <video> o el
    // <audio> siguen sonando detrás del modal cerrado.
    const previewBody = document.getElementById('preview-body');
    if (id === 'preview-overlay' && previewBody) {
      previewBody.innerHTML = '';
    }
  }

  function openModal(id) {
    const overlay = document.getElementById(id);
    if (overlay) overlay.classList.add('open');
  }

  // ── Vista previa ───────────────────────────────────────────────────────────
  function openPreview(fileId, mimeType, name) {
    const body = document.getElementById('preview-body');
    if (!body) return;
    const url = '/preview/' + encodeURIComponent(fileId);
    body.innerHTML = '';

    let element;
    if (mimeType.indexOf('image/') === 0) {
      element = document.createElement('img');
      element.src = url;
      element.alt = name;
    } else if (mimeType === 'application/pdf') {
      element = document.createElement('embed');
      element.src = url;
      element.type = 'application/pdf';
    } else if (mimeType.indexOf('audio/') === 0 || mimeType.indexOf('video/') === 0) {
      element = document.createElement(mimeType.indexOf('audio/') === 0 ? 'audio' : 'video');
      element.src = url;
      element.controls = true;
      element.className = 'preview-media';
    } else {
      element = document.createElement('iframe');
      element.src = url;
    }
    body.appendChild(element);

    const title = document.getElementById('preview-title');
    if (title) title.textContent = 'Vista previa — ' + name;
    openModal('preview-overlay');
  }

  function openProps(data) {
    const campos = {
      'prop-name': data.name,
      'prop-uploader': data.uploader,
      'prop-visibility': data.visibility === 'public' ? 'Público' : 'Privado',
      'prop-size': data.size,
      'prop-mime': data.mime,
      'prop-date': data.date
    };
    Object.keys(campos).forEach(function (id) {
      const celda = document.getElementById(id);
      // textContent y no innerHTML: el nombre del fichero lo elige quien sube,
      // así que es texto ajeno y no puede acabar interpretándose como HTML.
      if (celda) celda.textContent = campos[id] || '';
    });
    openModal('props-overlay');
  }

  // ── Lista de destinatarios (subida y permisos) ─────────────────────────────
  // Solo se muestra al elegir "privado — seleccionar personas".
  function sincronizarListaUsuarios() {
    const lista = document.getElementById('user-list');
    if (!lista) return;
    const elegido = document.querySelector('input[name="visibility"]:checked');
    lista.classList.toggle('hidden', !elegido || elegido.value !== 'private_select');
  }

  // ── Subida con barra de progreso ───────────────────────────────────────────
  function formatBytes(bytes) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let size = bytes, i = 0;
    while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
    return size.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
  }

  function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '--';
    seconds = Math.round(seconds);
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return m > 0 ? m + ' min ' + s + ' s' : s + ' s';
  }

  function prepararSubida(form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();

      const progressBox = document.getElementById('upload-progress');
      const fill = document.getElementById('upload-progress-fill');
      const pctLabel = document.getElementById('upload-progress-pct');
      const speedLabel = document.getElementById('upload-progress-speed');
      const etaLabel = document.getElementById('upload-progress-eta');
      const submitBtn = document.getElementById('upload-btn');

      progressBox.classList.remove('hidden');
      submitBtn.disabled = true;

      const formData = new FormData(form);
      const xhr = new XMLHttpRequest();
      const startTime = Date.now();

      xhr.upload.addEventListener('progress', function (evt) {
        if (!evt.lengthComputable) return;
        const elapsedSec = (Date.now() - startTime) / 1000;
        const pct = (evt.loaded / evt.total) * 100;
        const speed = elapsedSec > 0 ? evt.loaded / elapsedSec : 0;
        const eta = speed > 0 ? (evt.total - evt.loaded) / speed : Infinity;

        fill.style.width = pct.toFixed(1) + '%';
        pctLabel.textContent = pct.toFixed(0) + '% (' + formatBytes(evt.loaded) + ' / ' + formatBytes(evt.total) + ')';
        speedLabel.textContent = speed > 0 ? formatBytes(speed) + '/s' : '';
        etaLabel.textContent = pct >= 100 ? 'Finalizando…' : 'Tiempo estimado: ' + formatTime(eta);
      });

      xhr.addEventListener('load', function () {
        if (xhr.status >= 200 && xhr.status < 400) {
          window.location.href = xhr.responseURL || form.dataset.doneUrl || '/';
          return;
        }
        submitBtn.disabled = false;
        // 413 es el rechazo por tamaño del propio servidor: merece un mensaje
        // concreto, porque "error al subir" tras diez minutos de barra no dice
        // nada de qué hacer a continuación.
        etaLabel.textContent = xhr.status === 413
          ? 'El archivo supera el límite de subida del servidor.'
          : 'Error al subir el archivo (código ' + xhr.status + ').';
      });

      xhr.addEventListener('error', function () {
        submitBtn.disabled = false;
        etaLabel.textContent = 'Error de red durante la subida.';
      });

      xhr.open('POST', form.action || window.location.href, true);
      xhr.send(formData);
    });
  }

  // ── Enganches ──────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    // Un flash recién renderizado se muestra solo.
    if (document.getElementById('flash-overlay')) openModal('flash-overlay');

    // Delegación en el documento: los botones de las filas de la tabla se
    // pintan en bucle, y así no hace falta un listener por fila.
    document.addEventListener('click', function (e) {
      const target = e.target.closest('[data-action]');
      if (!target) return;
      const accion = target.dataset.action;

      if (accion === 'close-modal') {
        closeModal(target.dataset.modal);
      } else if (accion === 'preview') {
        openPreview(target.dataset.fileId, target.dataset.mime, target.dataset.name);
      } else if (accion === 'props') {
        openProps(target.dataset);
      }
    });

    // Cerrar pinchando fuera del cuadro o con Escape: antes solo se podía con
    // la X, que en el móvil queda lejos del pulgar.
    document.addEventListener('click', function (e) {
      if (e.target.classList && e.target.classList.contains('modal-overlay')) {
        closeModal(e.target.id);
      }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      document.querySelectorAll('.modal-overlay.open').forEach(function (o) {
        closeModal(o.id);
      });
    });

    // Confirmaciones de formulario (borrar, resetear contraseña…).
    document.querySelectorAll('form[data-confirm]').forEach(function (form) {
      form.addEventListener('submit', function (e) {
        if (!window.confirm(form.dataset.confirm)) e.preventDefault();
      });
    });

    // Radios de visibilidad.
    document.querySelectorAll('input[name="visibility"]').forEach(function (radio) {
      radio.addEventListener('change', sincronizarListaUsuarios);
    });
    sincronizarListaUsuarios();

    // Nombres de los ficheros elegidos.
    const fileInput = document.getElementById('file-input');
    if (fileInput) {
      fileInput.addEventListener('change', function (e) {
        const list = document.getElementById('file-list');
        list.innerHTML = '';
        Array.from(e.target.files).forEach(function (file) {
          const li = document.createElement('li');
          li.textContent = file.name + ' (' + formatBytes(file.size) + ')';
          list.appendChild(li);
        });
      });
    }

    const uploadForm = document.getElementById('upload-form');
    if (uploadForm) prepararSubida(uploadForm);
  });
})();
