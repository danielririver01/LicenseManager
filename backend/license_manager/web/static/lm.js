/* License Manager - utilidades de interfaz.
 *
 * El sitio corre con CSP `script-src 'self'`: no se permite ningun script
 * inline, ni siquiera un `onclick`. Por eso la copia de la licencia vive aqui
 * y los botones solo declaran `data-copiar` con el id del nodo a copiar.
 */
(function () {
  "use strict";

  function textoDe(id) {
    var nodo = document.getElementById(id);
    return nodo ? nodo.textContent.trim() : "";
  }

  function copiarPortapapeles(texto) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(texto);
    }
    // Respaldo para navegadores sin la API de portapapeles.
    return new Promise(function (resolver, rechazar) {
      var area = document.createElement("textarea");
      area.value = texto;
      area.setAttribute("readonly", "readonly");
      area.style.position = "fixed";
      area.style.top = "-1000px";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (err) {
        ok = false;
      }
      document.body.removeChild(area);
      if (ok) {
        resolver();
      } else {
        rechazar(new Error("no-copy"));
      }
    });
  }

  function avisar(mensaje, tono) {
    var zona = document.querySelector("[data-estado-copia]");
    if (!zona) {
      window.alert(mensaje);
      return;
    }
    zona.textContent = mensaje;
    zona.className = "alerta alerta-" + tono;
    zona.style.display = "block";
  }

  function prepararBotones() {
    var botones = document.querySelectorAll("[data-copiar]");
    Array.prototype.forEach.call(botones, function (boton) {
      boton.addEventListener("click", function () {
        var texto = textoDe(boton.getAttribute("data-copiar"));
        if (!texto) {
          return;
        }
        var original = boton.textContent;
        copiarPortapapeles(texto)
          .then(function () {
            boton.textContent = "Copiado";
            avisar("Texto de licencia FRB1 copiado al portapapeles.", "ok");
            window.setTimeout(function () {
              boton.textContent = original;
            }, 2500);
          })
          .catch(function () {
            avisar(
              "El navegador no permitio copiar. Selecciona el texto de la " +
                "licencia y copialo manualmente (Ctrl+C).",
              "aviso"
            );
          });
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", prepararBotones);
  } else {
    prepararBotones();
  }
})();
