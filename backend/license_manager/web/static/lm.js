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

  /* --------------------------------------------------------------------- *
   * Emision de licencias (UI-05).
   *
   * El codigo que pega el tecnico se formatea en vivo como XXXX-XXXX-XXXX-XXXX
   * y se valida antes de enviar: 16 hex, sin excepciones. El servidor vuelve a
   * validar lo mismo; esto existe para que el operador se entere en el momento
   * y no de ida y vuelta.
   * --------------------------------------------------------------------- */

  var SOLO_HEX = /[^0-9a-fA-F]/g;

  function despieceCodigo(valor) {
    var limpio = String(valor || "").replace(SOLO_HEX, "").toUpperCase().slice(0, 16);
    return {
      limpio: limpio,
      texto: limpio.replace(/(.{4})(?=.)/g, "$1-"),
    };
  }

  function prepararEmision() {
    var campo = document.querySelector("[data-codigo-equipo]");
    if (!campo || !campo.form) {
      return;
    }

    var formulario = campo.form;
    var hint = document.querySelector("[data-codigo-hint]");
    var resumenCodigo = document.querySelector("[data-resumen-codigo]");
    var resumenCliente = document.querySelector("[data-resumen-cliente]");
    var selector = document.querySelector("[data-cliente-select]");
    var zonaError = document.querySelector("[data-error-formulario]");
    var botonLimpiar = document.querySelector("[data-limpiar-emision]");

    function ocultarError() {
      if (zonaError) {
        zonaError.textContent = "";
        zonaError.classList.add("oculto");
      }
    }

    function mostrarError(mensaje, foco) {
      if (zonaError) {
        zonaError.textContent = mensaje;
        zonaError.classList.remove("oculto");
      }
      if (foco) {
        foco.focus();
      }
    }

    function pintar() {
      var pieza = despieceCodigo(campo.value);
      if (campo.value !== pieza.texto) {
        campo.value = pieza.texto;
      }

      if (resumenCodigo) {
        resumenCodigo.textContent =
          pieza.limpio.length === 16 ? pieza.texto : "----------------";
      }

      if (hint) {
        if (pieza.limpio.length === 16) {
          hint.textContent = "✓ Codigo de 16 caracteres hexadecimales listo para firmar.";
          hint.className = "pista hint-codigo valido";
        } else if (pieza.limpio.length > 0) {
          hint.textContent =
            "Faltan " + (16 - pieza.limpio.length) +
            " caracteres para completar los 16 hex.";
          hint.className = "pista hint-codigo incompleto";
        } else {
          hint.textContent =
            "Pega el codigo que muestra Frubber POS en la pantalla de " +
            "activacion del computador.";
          hint.className = "pista hint-codigo";
        }
      }

      if (selector && resumenCliente) {
        var opcion = selector.options[selector.selectedIndex];
        resumenCliente.textContent =
          opcion && opcion.value
            ? opcion.getAttribute("data-nombre") || opcion.textContent
            : "—";
      }

      ocultarError();
    }

    campo.addEventListener("input", pintar);
    if (selector) {
      selector.addEventListener("change", pintar);
    }

    formulario.addEventListener("submit", function (evento) {
      var pieza = despieceCodigo(campo.value);
      if (pieza.limpio.length !== 16) {
        evento.preventDefault();
        mostrarError(
          "El codigo de equipo debe tener exactamente 16 caracteres " +
            "hexadecimales (0-9, A-F).",
          campo
        );
        return;
      }
      if (selector && !selector.value) {
        evento.preventDefault();
        mostrarError(
          "Elige la verduleria o negocio al que pertenece la maquina.",
          selector
        );
        return;
      }
      ocultarError();
    });

    if (botonLimpiar) {
      botonLimpiar.addEventListener("click", function () {
        formulario.reset();
        pintar();
        campo.focus();
      });
    }

    pintar();
  }

  /* --------------------------------------------------------------------- *
   * Ingresos y ciclo de cobro (UI-07).
   *
   * Dos comportamientos, sin ningun script inline (CSP script-src 'self'):
   * el modal de "Actualizar estado de cuenta" y la confirmacion previa a las
   * acciones que congelan el cronometro de una cuenta.
   * --------------------------------------------------------------------- */

  function prepararConfirmaciones() {
    var botones = document.querySelectorAll("[data-confirmar]");
    Array.prototype.forEach.call(botones, function (boton) {
      boton.addEventListener("click", function (evento) {
        var texto = boton.getAttribute("data-confirmar");
        if (texto && !window.confirm(texto)) {
          evento.preventDefault();
        }
      });
    });
  }

  function prepararModalCuentas() {
    var modal = document.querySelector("[data-modal-cuenta]");
    if (!modal) {
      return;
    }

    var formulario = modal.querySelector("[data-form-cuenta]");
    var campoCliente = modal.querySelector("[data-campo-cliente]");
    var campoEstado = modal.querySelector("[data-campo-estado]");
    var campoImporte = modal.querySelector("[data-campo-importe]");
    var campoMetodo = modal.querySelector("[data-campo-metodo]");
    var campoNota = modal.querySelector("[data-campo-nota]");

    function abrir(datos) {
      if (formulario && datos.accion) {
        formulario.setAttribute("action", datos.accion);
      }
      if (campoCliente) {
        campoCliente.value = datos.cliente || "";
      }
      if (campoEstado) {
        campoEstado.value = datos.cobro || "pagado";
      }
      if (campoImporte) {
        campoImporte.value = "";
      }
      if (campoMetodo) {
        campoMetodo.value = "";
      }
      if (campoNota) {
        campoNota.value = "";
      }
      modal.classList.remove("oculto");
      if (campoEstado) {
        campoEstado.focus();
      }
    }

    function cerrar() {
      modal.classList.add("oculto");
    }

    var botonesAbrir = document.querySelectorAll("[data-abrir-cuenta]");
    Array.prototype.forEach.call(botonesAbrir, function (boton) {
      boton.addEventListener("click", function () {
        abrir({
          accion: boton.getAttribute("data-accion"),
          cliente: boton.getAttribute("data-cliente"),
          cobro: boton.getAttribute("data-cobro"),
        });
      });
    });

    var botonesCerrar = modal.querySelectorAll("[data-cerrar-cuenta]");
    Array.prototype.forEach.call(botonesCerrar, function (boton) {
      boton.addEventListener("click", cerrar);
    });

    // El modal se cierra tambien al pulsar Escape, sin salir del formulario.
    document.addEventListener("keydown", function (evento) {
      if (evento.key === "Escape" && !modal.classList.contains("oculto")) {
        cerrar();
      }
    });
  }

  /* El importe de un ingreso se admite solo como entero de COP, igual que en
   * la emision: ``450000`` o ``450.000``. Un decimal se rechaza antes de ir
   * al servidor para que el operador lo corrija en el momento. */
  var IMPORTE_VALIDO = /^\d{1,3}(?:[.,]\d{3})*$/;

  function prepararImportes() {
    var campos = document.querySelectorAll("[data-importe-ingreso]");
    Array.prototype.forEach.call(campos, function (campo) {
      var formulario = campo.form;
      if (!formulario) {
        return;
      }
      formulario.addEventListener("submit", function (evento) {
        var valor = String(campo.value || "").trim();
        if (valor && !IMPORTE_VALIDO.test(valor)) {
          evento.preventDefault();
          window.alert(
            "El importe debe ser un numero entero de pesos, sin centavos " +
              "(por ejemplo 450000 o 450.000)."
          );
          campo.focus();
        }
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      prepararBotones();
      prepararEmision();
      prepararConfirmaciones();
      prepararModalCuentas();
      prepararImportes();
    });
  } else {
    prepararBotones();
    prepararEmision();
    prepararConfirmaciones();
    prepararModalCuentas();
    prepararImportes();
  }
})();
