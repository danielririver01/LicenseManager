-- Trigger de defensa en profundidad sobre la auditoria (JHO-105).
--
-- La cadena de hash ya permite detectar cualquier manipulacion, pero en
-- PostgreSQL vamos un paso mas alla: la tabla fisicamente no acepta UPDATE ni
-- DELETE, ni siquiera para quien tiene sesion en el servidor. Solo se puede
-- INSERT.
--
-- SQLite no ejecuta este script; alla el bloqueo lo pone la capa de
-- aplicacion y la cadena de hash.

CREATE OR REPLACE FUNCTION auditoria_bloquear_edicion()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'La tabla de auditoria es de solo escritura (append-only): % no esta permitido sobre %.',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = '42501';
END;
$$;

DROP TRIGGER IF EXISTS trg_auditoria_bloquear ON auditoria;

CREATE TRIGGER trg_auditoria_bloquear
BEFORE UPDATE OR DELETE ON auditoria
FOR EACH ROW EXECUTE FUNCTION auditoria_bloquear_edicion();

-- Tambien se bloquea TRUNCATE, que vaciaria la tabla sin tocar filas.
REVOKE TRUNCATE ON auditoria FROM PUBLIC;
