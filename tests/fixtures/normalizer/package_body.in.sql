create   or
replace   PACKAGE BODY employee_pkg as
	-- keep the CACHE warm, and mind the TABLESPACE here
	g_label constant varchar2(30) := 'STORAGE(INITIAL 64K)';


	procedure hire(p_name in varchar2) is
	begin
		insert into employees(name) values (p_name);
	end hire;

	function label return varchar2 is
	begin
		return q'[NOLOGGING and PCTFREE 10]';
	end label;
end employee_pkg;
