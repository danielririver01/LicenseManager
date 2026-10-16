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

  /* --------------------------------------------------------------------- *
   * Liquidación de utilidades (UI-11, JHO-103).
   *
   * Tres comportamientos, todos sin script inline (CSP script-src 'self'):
   * la calculadora en vivo, el modal "Registrar Pago de Comisión" y el
   * botón de imprimir el soporte.
   * --------------------------------------------------------------------- */

  /* La aritmética replica la del servidor: pesos enteros, porcentaje
   * redondeado al peso más cercano con `(bruto*valor + 50) // 100`. */
  function calculoLicencias(texto) {
    var limpio = String(texto || "").replace(/[^0-9]/g, "");
    return limpio ? parseInt(limpio, 10) : 0;
  }

  function calculoValor(texto) {
    var limpio = String(texto || "").replace(/[^0-9]/g, "");
    return limpio ? parseInt(limpio, 10) : 0;
  }

  function prepararCalculadora() {
    var formulario = document.querySelector("[data-form-liquidacion]");
    if (!formulario) {
      return;
    }

    var precio = parseInt(formulario.getAttribute("data-precio-venta") || "450000", 10);
    var campoLicencias = formulario.querySelector("[data-calc-licencias]");
    var campoTipo = formulario.querySelector("[data-calc-tipo]");
    var campoValor = formulario.querySelector("[data-calc-valor]");
    var etiquetaValor = formulario.querySelector("[data-calc-etiqueta]");
    var pistaValor = formulario.querySelector("[data-calc-pista]");
    var selectorAdmin = formulario.querySelector("[data-calc-admin]");
    var resBruto = document.querySelector("[data-res-bruto]");
    var resBrutoDetalle = document.querySelector("[data-res-bruto-detalle]");
    var resComision = document.querySelector("[data-res-comision]");
    var resComisionDetalle = document.querySelector("[data-res-comision-detalle]");
    var resNeta = document.querySelector("[data-res-neta]");
    var resNetaDetalle = document.querySelector("[data-res-neta-detalle]");

    function formatoCop(monto) {
      return "$" + monto.toLocaleString("es-CO") + " COP";
    }

    function pintar() {
      var licencias = calculoLicencias(campoLicencias ? campoLicencias.value : "");
      var tipo = campoTipo ? campoTipo.value : "porcentaje";
      var valor = calculoValor(campoValor ? campoValor.value : "");
      var bruto = licencias * precio;
      var comision = 0;

      if (tipo === "porcentaje") {
        if (etiquetaValor) {
          etiquetaValor.textContent = "4. Porcentaje de comisión (%)";
        }
        if (pistaValor) {
          pistaValor.textContent = "Entero entre 1 y 100. Se redondea al peso.";
        }
        comision = valor > 0 ? Math.floor((bruto * valor + 50) / 100) : 0;
        if (resComisionDetalle) {
          resComisionDetalle.textContent =
            valor + "% (" + formatoCop(licencias > 0 ? Math.floor(comision / licencias) : 0) + " c/u)";
        }
      } else {
        if (etiquetaValor) {
          etiquetaValor.textContent = "4. Valor fijo en pesos ($ COP)";
        }
        if (pistaValor) {
          pistaValor.textContent = "Entero en pesos por licencia, sin centavos.";
        }
        comision = licencias * valor;
        if (resComisionDetalle) {
          resComisionDetalle.textContent = formatoCop(valor) + " fijo por licencia";
        }
      }

      // Una comisión mayor que el bruto es un error de digitación: se avisa
      // en vivo y el servidor la rechaza igual.
      var excede = comision > bruto;
      if (resBruto) {
        resBruto.textContent = formatoCop(bruto);
      }
      if (resBrutoDetalle) {
        resBrutoDetalle.textContent = licencias + " licencia" + (licencias === 1 ? "" : "s") +
          " × " + formatoCop(precio);
      }
      if (resComision) {
        resComision.textContent = formatoCop(comision);
        resComision.style.color = excede ? "var(--rojo-peligro)" : "var(--verde-activo)";
      }
      if (resNeta) {
        resNeta.textContent = formatoCop(bruto - comision);
        resNetaDetalle.textContent = excede
          ? "La comisión no puede superar el total recaudado."
          : "Retenido para la sociedad";
      }
    }

    // Al elegir una persona se precarga lo que cobró en el mes.
    if (selectorAdmin) {
      selectorAdmin.addEventListener("change", function () {
        var opcion = selectorAdmin.options[selectorAdmin.selectedIndex];
        var licencias = opcion ? opcion.getAttribute("data-licencias") : null;
        if (campoLicencias && licencias && parseInt(licencias, 10) > 0) {
          campoLicencias.value = licencias;
        }
        pintar();
      });
    }

    [campoLicencias, campoTipo, campoValor].forEach(function (campo) {
      if (campo) {
        campo.addEventListener("input", pintar);
        campo.addEventListener("change", pintar);
      }
    });

    // Los campos se envían limpios: solo dígitos, igual que en el servidor.
    formulario.addEventListener("submit", function (evento) {
      var valorTipo = campoTipo ? campoTipo.value : "porcentaje";
      var permitido = valorTipo === "porcentaje" ? /^(100|[1-9][0-9]?)$/ : /^[1-9][0-9]*$/;
      if (campoLicencias && !/^[1-9][0-9]*$/.test(String(campoLicencias.value || "").trim())) {
        evento.preventDefault();
        window.alert("La cantidad de licencias debe ser un número entero de 1 en adelante.");
        campoLicencias.focus();
        return;
      }
      if (campoValor && !permitido.test(String(campoValor.value || "").trim())) {
        evento.preventDefault();
        window.alert(
          valorTipo === "porcentaje"
            ? "El porcentaje de comisión debe ser un número entero entre 1 y 100."
            : "El valor fijo de la comisión debe ser un número entero en pesos, mayor que cero."
        );
        campoValor.focus();
      }
    });

    pintar();
  }

  function prepararModalPago() {
    var modal = document.querySelector("[data-modal-pago]");
    if (!modal) {
      return;
    }

    var formulario = modal.querySelector("[data-form-pago]");
    var campoOperador = modal.querySelector("[data-campo-operador]");
    var campoMonto = modal.querySelector("[data-campo-monto]");
    var campoMetodo = modal.querySelector("[data-campo-metodo]");
    var campoComprobante = modal.querySelector("[data-campo-comprobante]");

    function abrir(datos) {
      if (formulario && datos.accion) {
        formulario.setAttribute("action", datos.accion);
      }
      if (campoOperador) {
        campoOperador.value = datos.beneficiario || "";
      }
      if (campoMonto) {
        campoMonto.value = datos.comision || "";
      }
      if (campoMetodo) {
        campoMetodo.value = "";
      }
      if (campoComprobante) {
        campoComprobante.value = "";
      }
      modal.classList.remove("oculto");
      if (campoMetodo) {
        campoMetodo.focus();
      }
    }

    function cerrar() {
      modal.classList.add("oculto");
    }

    var botonesAbrir = document.querySelectorAll("[data-abrir-pago]");
    Array.prototype.forEach.call(botonesAbrir, function (boton) {
      boton.addEventListener("click", function () {
        abrir({
          accion: boton.getAttribute("data-accion"),
          beneficiario: boton.getAttribute("data-beneficiario"),
          comision: boton.getAttribute("data-comision"),
        });
      });
    });

    var botonesCerrar = modal.querySelectorAll("[data-cerrar-pago]");
    Array.prototype.forEach.call(botonesCerrar, function (boton) {
      boton.addEventListener("click", cerrar);
    });

    document.addEventListener("keydown", function (evento) {
      if (evento.key === "Escape" && !modal.classList.contains("oculto")) {
        cerrar();
      }
    });

    // Reabrir el modal tras un rechazo o tras "Aplicar y Pagar".
    if (modal.getAttribute("data-reabrir")) {
      modal.classList.remove("oculto");
    }
  }

  function prepararImpresion() {
    var botones = document.querySelectorAll("[data-imprimir]");
    Array.prototype.forEach.call(botones, function (boton) {
      boton.addEventListener("click", function () {
        window.print();
      });
    });
  }

  /* --------------------------------------------------------------------- *
   * Restablecer contrasena de una cuenta (UI-09).
   *
   * El servidor exige la contrasena nueva: el formulario de la tabla no la
   * llevaba, asi que la peticion se rechazaba. Se pide en un modal, igual que
   * en UI-07, y sin ningun script inline (CSP script-src 'self').
   * --------------------------------------------------------------------- */

  function prepararModalRestablecer() {
    var modal = document.querySelector("[data-modal-restablecer]");
    if (!modal) {
      return;
    }

    var formulario = modal.querySelector("[data-form-restablecer]");
    var campoNombre = modal.querySelector("[data-campo-nombre]");
    var campoClave = modal.querySelector("[data-campo-clave]");
    var campoClave2 = modal.querySelector("[data-campo-clave2]");

    function abrir(datos) {
      if (formulario && datos.accion) {
        formulario.setAttribute("action", datos.accion);
      }
      if (campoNombre) {
        campoNombre.value = datos.nombre || "";
      }
      if (campoClave) {
        campoClave.value = "";
      }
      if (campoClave2) {
        campoClave2.value = "";
      }
      modal.classList.remove("oculto");
      if (campoClave) {
        campoClave.focus();
      }
    }

    function cerrar() {
      modal.classList.add("oculto");
    }

    var botonesAbrir = document.querySelectorAll("[data-abrir-restablecer]");
    Array.prototype.forEach.call(botonesAbrir, function (boton) {
      boton.addEventListener("click", function () {
        abrir({
          accion: boton.getAttribute("data-accion"),
          nombre: boton.getAttribute("data-nombre"),
        });
      });
    });

    var botonesCerrar = modal.querySelectorAll("[data-cerrar-restablecer]");
    Array.prototype.forEach.call(botonesCerrar, function (boton) {
      boton.addEventListener("click", cerrar);
    });

    document.addEventListener("keydown", function (evento) {
      if (evento.key === "Escape" && !modal.classList.contains("oculto")) {
        cerrar();
      }
    });

    if (formulario) {
      formulario.addEventListener("submit", function (evento) {
        var clave = campoClave ? String(campoClave.value || "") : "";
        if (clave.length < 10) {
          evento.preventDefault();
          window.alert("La contrasena debe tener al menos 10 caracteres.");
          if (campoClave) {
            campoClave.focus();
          }
          return;
        }
        if (campoClave2 && campoClave2.value !== clave) {
          evento.preventDefault();
          window.alert("Las dos contrasenas deben ser iguales.");
          campoClave2.focus();
        }
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      prepararBotones();
      prepararEmision();
      prepararConfirmaciones();
      prepararModalCuentas();
      prepararImportes();
      prepararCalculadora();
      prepararModalPago();
      prepararImpresion();
      prepararModalRestablecer();
    });
  } else {
    prepararBotones();
    prepararEmision();
    prepararConfirmaciones();
    prepararModalCuentas();
    prepararImportes();
    prepararCalculadora();
    prepararModalPago();
    prepararImpresion();
    prepararModalRestablecer();
  }
})();
