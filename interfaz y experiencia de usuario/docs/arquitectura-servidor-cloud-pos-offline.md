# FRUBBER POS — Arquitectura Actualizada: Servidor Web Cloud (VPS) + POS Offline
**Versión:** 3.0  
**Fecha:** 15 de octubre de 2026  
**Entorno de License Manager:** Servidor Web Cloud / VPS (HTTPS con dominio privado)  
**Entorno de Frubber POS:** 100% Offline (Local en verdulería / PC de caja)

---

## 1. Principio Fundamental de la Arquitectura

```
┌───────────────────────────────────────────────┐
│              EN LA NUBE (CLOUD)               │
│         License Manager (Web Privada)         │
│                                               │
│  - Servidor Cloud / VPS Linux con HTTPS       │
│  - Clave privada ECDSA P-256 en entorno seguro│
│  - Base de datos de clientes, cobros y cupos  │
│  - Acceso desde PC o Celular de los Socios    │
└───────────────────────┬───────────────────────┘
                        │
       El técnico envía el código (WhatsApp/Web)
       El sistema emite la licencia FRB1
                        │
                        ▼
┌───────────────────────────────────────────────┐
│            EN LA VERDULERÍA (LOCAL)           │
│           Frubber POS (100% Offline)          │
│                                               │
│  - PC de caja + Balanza serial COM/USB        │
│  - CERO dependencia de internet en la caja    │
│  - El técnico ingresa la cadena firmada FRB1  │
│  - La clave pública valida y activa de por    │
│    vida el sistema en la máquina local        │
└───────────────────────────────────────────────┘
```

---

## 2. Componentes y Despliegue en la Nube

### A. Servidor Web Cloud / VPS
* **Infraestructura:** VPS Linux (Ubuntu Server) en DigitalOcean, AWS, Hetzner u Oracle Cloud.
* **Seguridad de Red:**
  * Acceso web exclusivamente bajo **SSL/TLS (HTTPS)** con certificado Let's Encrypt o Cloudflare.
  * Autenticación basada en cookies HttpOnly y protección CSRF.
  * Sin registros públicos abiertos: solo tú (Titular Principal) puedes invitar o crear al Co-Titular (Socio) y Administradores.
* **Custodia Criptográfica:**
  * La **clave privada maestra ECDSA P-256** se almacena en variables de entorno seguras del sistema operativo o servicio de secretos (AWS KMS / Azure Key Vault / HashiCorp Vault).
  * El frontend y los usuarios nunca tienen acceso a la clave privada; solo el proceso backend firma las licencias en memoria al recibir una solicitud autorizada.
* **Base de Datos Gestionada:**
  * PostgreSQL o SQLite protegido con backups diarios automatizados a almacenamiento de objetos (Amazon S3 / Backblaze B2).

---

## 3. Flujo en la Calle (En el Establecimiento)

1. **Instalación:** El técnico balancero llega a la verdulería, instala Frubber POS y conecta la balanza al puerto COM.
2. **Generación del Código:** Frubber POS lee la placa base y procesador del equipo y muestra en pantalla: `CÓDIGO DE HARDWARE: 7B4F-892E-0C1A-5D34`.
3. **Solicitud de Licencia:** El técnico abre License Manager desde su celular (o le manda los 16 caracteres a los socios por WhatsApp).
4. **Firma Criptográfica Remota:**
   * En License Manager web se valida el código.
   * Se descuenta 1 cupo del paquete mayorista prepago del técnico.
   * El backend firma digitalmente y genera el bloque `FRB1-...`.
5. **Activación Offline:** El técnico pega o digita la clave `FRB1` en el computador de la verdulería. Frubber POS valida la firma con la clave pública embebida y se desbloquea de por vida **sin haber tenido internet ni un solo segundo**.

---

## 4. Tareas Sincronizadas en Linear

* **[JHO-97](https://linear.app/jhoan-rivera/issue/JHO-97):** Arquitectura Base: Servidor Web Cloud (VPS Linux HTTPS) y Custodia Criptográfica ECDSA P-256.
* **[JHO-98](https://linear.app/jhoan-rivera/issue/JHO-98):** Autenticación y Setup del Titular Principal.
* **[JHO-99](https://linear.app/jhoan-rivera/issue/JHO-99):** Gestión de Administradores, Seguridad de Clave y Alta de Socios.
* **[JHO-100](https://linear.app/jhoan-rivera/issue/JHO-100):** Directorio de Verdulerías e Inventario de Máquinas.
* **[JHO-101](https://linear.app/jhoan-rivera/issue/JHO-101):** Motor de Emisión y Firma Remota de Licencias `FRB1`.
* **[JHO-102](https://linear.app/jhoan-rivera/issue/JHO-102):** Cobros Manuales COP y Ciclo de Prueba.
* **[JHO-103](https://linear.app/jhoan-rivera/issue/JHO-103):** Cierres Contables y Reparto de Utilidades entre Socios.
* **[JHO-104](https://linear.app/jhoan-rivera/issue/JHO-104):** Módulo de Paquetes Mayoristas para Técnicos Balanceros.
* **[JHO-105](https://linear.app/jhoan-rivera/issue/JHO-105):** Auditoría Inmutable de Eventos.
* **[JHO-106](https://linear.app/jhoan-rivera/issue/JHO-106):** Pruebas E2E, Despliegue en Servidor Cloud y Backups Diarios.
