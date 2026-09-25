-- 官方导出里每张表都有 "OWNER TO xiaolongli"。这个角色不存在的话，初始化会在第一张表就停下。
CREATE ROLE xiaolongli NOLOGIN;
