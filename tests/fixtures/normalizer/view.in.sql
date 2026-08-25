CREATE OR REPLACE VIEW "HR"."V_ACTIVE" AS
select e.id,
       e.name,
       /* NOLOGGING is fine in here */
       e."Status"
  from employees e
 where e.status = 'CACHE'
