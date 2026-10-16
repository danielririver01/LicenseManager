# Manual de Operación y Contingencia

Para el **Titular Principal** y su **Socio (Co-Titular)**. Explica qué hacer
cada día, cómo cuidar la llave, cómo comprobar que la información está sana y
qué hacer cuando algo falla.

Los detalles técnicos del servidor están en `deploy/README.md`.

---

## 1. Lo que hay que entender de memoria

1. **El POS funciona sin internet.** La verdulería nunca depende del sitio
   mientras vende: la licencia se verifica con la llave pública que el POS ya
   trae embebida.
2. **Este sitio solo firma.** No habla con el POS ni con la balanza. El técnico
   manda el código de 16 caracteres por WhatsApp y aquí se convierte en una
   cadena `FRB1-...` que se pega una sola vez en el computador del negocio.
3. **La llave privada vive solo en el servidor.** No está en ningún repositorio,
   no se copia al celular y no se comparte por chat.

## 2. Quién puede hacer qué

| Módulo | Titular Principal | Co-Titular (Socio) | Administrador Delegado | Técnico |
| --- | :---: | :---: | :---: | :---: |
| Crear usuarios y socios (Administradores) | Sí | Sí | **No (403)** | **No (403)** |
| Liquidación y utilidades | Sí | Sí | **No (403)** | **No (403)** |
| Historial de auditoría | Sí | Sí | Lectura básica | **No (403)** |
| Comprar paquetes mayoristas | Sí | Sí | **No (403)** | **No (403)** |
| Dar acceso a un técnico | Sí | Sí | **No (403)** | **No (403)** |
| Ver el saldo de los técnicos | Sí | Sí | Sí | **Solo el suyo** |
| Emitir licencias | Sí | Sí | Sí | **Solo con su saldo** |
| Registrar verdulerías, equipos y cobros | Sí | Sí | Sí | Verdulerías y equipos |

### El acceso del técnico (JHO-107)

Un técnico balancero puede tener **cuenta propia**, atada a su ficha y a su
saldo prepago. Se la crea el Titular desde **Mayorista → Dar acceso**.

* Entra cuando quiere y emite solo, sin llamar a nadie.
* Cada venta le descuenta **un cupo de su paquete** (nada más se le cobra).
* Ve **su** saldo, **sus** paquetes y **sus** emisiones. No ve a los demás
  técnicos ni el dinero de la sociedad.
* Puede **recargar sus cupos él mismo** con Mercado Pago (tarjeta, PSE, Nequi o
  efectivo) y los cupos entran solos al confirmarse el pago. Si esa opción no
  está configurada, pide el paquete a la sociedad.
* Puede emitir **venta**, **prueba** y **reemplazo** justificado. **No** puede
  regalar cortesías: esas las autoriza la sociedad.
* Cuando se queda sin cupos, recarga o pide un paquete nuevo. Sin saldo no puede
  vender.

Si un técnico se va, se desactiva su cuenta desde **Administradores**: pierde el
acceso al instante y su historial y sus licencias se conservan.

## 3. Operación diaria

### Emitir una licencia (el caso más frecuente)

1. El técnico envía por WhatsApp: **código de 16 caracteres** + nombre de la
   verdulería.
2. Entra a **Emitir licencia** → elige la verdulería → pega el código.
3. Si es una venta del paquete de un técnico, elige su nombre en
   *Técnico / paquete prepago*. El sistema **descuenta un cupo** y el importe
   no se cobra otra vez (el cupo ya se pagó).
4. Pulsa *Confirmar y firmar*, copia el bloque `FRB1` y mándalo de vuelta.
5. El técnico lo pega en el POS: queda activo de por vida en esa máquina.

> **Un código ya usado no vuelve a emitir.** Si quieres reemplazar una máquina
> dañada, elige el tipo **Reemplazo**, escribe la justificación del cambio
> físico (es obligatoria) y el sistema lo emite a **$0 COP**. Un reemplazo por
> garantía **no** gasta cupo del técnico.

### Registrar el dinero

* **Venta directa de la sociedad**: en *Ingresos*, registra el cobro con su
  método (Nequi, Daviplata, Bancolombia, efectivo u otro).
* **Paquete de un técnico**: en *Mayorista*, registra la compra del paquete.
  La sociedad cobra **todo el paquete por adelantado**; las emisiones
  posteriores de ese técnico ya no generan dinero.

### Revisar y cerrar el mes

* **Cierres mensuales**: ingresos cobrados, licencias, pendientes, promedio
  diario y comparativo semestral. Puedes **exportar el cierre** a CSV para
  Excel (sale con el formato colombiano y con el registro de quién exportó).
* **Liquidación**: calcula la comisión de un operador por porcentaje o por
  valor fijo, registra el pago con su comprobante y **sella** la liquidación.
  Una liquidación pagada ya no se toca: cualquier ajuste va al mes siguiente.

### Comprobar que nadie tocó el historial

Abre **Historial**. Arriba dice *"Íntegra"* (todo bien) o *"Rota"* (alguien
alteró el registro). El botón **Verificar cadena** deja constancia firmada de
la revisión, con la fecha y quién la hizo.

---

## 4. Cuidado de la llave privada

* Vive en el servidor, en un archivo con permisos restringidos. Es el único
  material que **no se puede reconstruir** a partir del sitio.
* **Nunca** la subas a Git, Drive, WhatsApp ni al correo.
* El `keyId` del servidor **debe** coincidir con el que el POS trae embebido
  (hoy `lic-v1-2026-09-29`). Si se cambia la llave, hay que actualizar el POS
  en **todos** los equipos antes de emitir: las licencias viejas seguirían
  válidas, pero las nuevas saldrían con otra firma.
* Guarda una copia de la llave **fuera del VPS** (por ejemplo, un gestor de
  contraseñas o un sobre sellado en custodia del Titular). Si el VPS se pierde
  y la llave también, no se podrán emitir más licencias hasta cambiar la llave
  en el POS.

---

## 5. Respaldos

* El servidor respalda la base **una vez al día** (03:30 UTC) y cada respaldo
  se **verifica** al crearse: se abre, se revisa su integridad y se recomputa la
  cadena de auditoría dentro de la copia.
* Se conservan los últimos **14** respaldos.
* Se copian además a almacenamiento en la nube (B2/S3) para sobrevivir a la
  pérdida del VPS.

### Cómo comprobar que los respaldos están corriendo (una vez al mes)

En el servidor:

```bash
sudo systemctl list-timers license-manager-backup.timer --no-pager
sudo journalctl -u license-manager-backup.service -n 10 --no-pager
ls -lh /var/backups/license-manager
```

Debe aparecer un respaldo por día con su `.sha256` al lado, y en el journal la
línea `... · verificado (integridad ok · cadena con N evento(s))`.

### Ensayo de restauración (una vez por trimestre)

Sigue la sección 7 de `deploy/README.md`. Al terminar, abre **Historial** en el
sitio restaurado: debe decir *"Íntegra"*.

---

## 6. Contingencias

| Síntoma | Qué significa | Qué hacer |
| --- | --- | --- |
| El POS dice que la licencia **no es válida** | La cadena se copió mal o el `keyId` no corresponde | Vuelve a copiar el bloque completo (desde `FRB1\|` hasta el final, sin espacios de más). Si sigue fallando, revisa el `keyId` del servidor contra el del POS |
| No aparece el código del POS | El POS no llegó a la pantalla de activación | Que el técnico verifique el POS y la balanza (puerto COM/USB) antes de pedir licencia |
| El sitio **no abre** (502 / no carga) | Caddy está bien pero el backend se cayó | `sudo systemctl status license-manager` y `sudo journalctl -u license-manager -n 50` |
| El sitio no abre y Caddy da error de certificado | DNS o puertos 80/443 | Revisa que el dominio apunte a la IP y que 80/443 estén abiertos |
| **El historial dice "Rota"** | Alguien alteró o borró filas de auditoría | **No borres nada.** Avisa al Titular Principal, conserva el respaldo del día y revisa quién tuvo acceso al servidor. Restaura el respaldo íntegro más reciente si hiciera falta |
| Se perdió la **llave privada** | No se pueden emitir licencias nuevas | Restaura la llave desde tu copia en custodia. Si no existe, hay que generar un par nuevo **y actualizar el POS** en todos los equipos |
| Un **administrador se va** de la empresa | Sigue teniendo acceso | *Administradores* → **Desactivar**. Se revocan sus sesiones al instante; su historial y sus emisiones se conservan |
| Un técnico se quedó **sin cupos** | Su saldo llegó a 0 | Registra la compra de un paquete nuevo en *Mayorista*. El técnico no puede emitir hasta entonces |
| Se registró un **cobro equivocado** | La auditoría no se edita | No trates de borrarlo: registra el movimiento que lo corrige (categoría de anulación/reembolso) y anota el motivo. Así queda explicado en el historial |
| Se perdió el **celular** de un socio | Sesión potencialmente expuesta | *Administradores* → desactivar o restablecer la clave de esa cuenta. Al cambiarla, se cierran todas sus sesiones |
| El **VPS fue comprometido** | La llave puede estar expuesta | Apaga el servicio, restaura un respaldo verificado en un servidor limpio, **genera una llave nueva y actualiza el POS**, y cambia todas las contraseñas |

---

## 7. Lo que nunca hay que hacer

* Editar la base de datos a mano: en PostgreSQL la tabla de auditoría lo
  rechaza, y en cualquier caso rompe la cadena y el historial lo delata.
* Compartir usuarios: cada persona tiene su cuenta para que el historial diga
  la verdad sobre quién hizo cada cosa.
* Emitir con una llave distinta a la que el POS trae embebida.
* Confiar en un respaldo que nunca se abrió. Para eso está la verificación y el
  ensayo de restauración.
