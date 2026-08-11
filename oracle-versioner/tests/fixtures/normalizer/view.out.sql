CREATE OR REPLACE VIEW HR.V_ACTIVE AS
SELECT e.id,
       e.name,
       /* NOLOGGING is fine in here */
       e."Status"
  FROM employees e
 WHERE e.status = 'CACHE';
