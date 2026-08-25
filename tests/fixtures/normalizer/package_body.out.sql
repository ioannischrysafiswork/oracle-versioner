CREATE OR REPLACE PACKAGE BODY employee_pkg AS
    -- keep the CACHE warm, and mind the TABLESPACE here
    g_label CONSTANT VARCHAR2(30) := 'STORAGE(INITIAL 64K)';

    PROCEDURE hire(p_name IN VARCHAR2) IS
    BEGIN
        INSERT INTO employees(name) VALUES (p_name);
    END hire;

    FUNCTION label RETURN VARCHAR2 IS
    BEGIN
        RETURN q'[NOLOGGING and PCTFREE 10]';
    END label;
END employee_pkg;
/
