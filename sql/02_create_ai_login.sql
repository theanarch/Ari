-- Creates the login the MCP server uses, with only the rights it needs.
-- Even if the server code had a bug, SQL Server itself refuses anything else:
-- no DELETE, no DROP, no TRUNCATE, no ALTER, and no access to other tables.
--
-- Requires "SQL Server and Windows Authentication mode" on the server
-- (SSMS: right-click the server > Properties > Security), then a restart of the SQL Server service.
-- Change the password before running.

USE master;
GO

IF SUSER_ID(N'ari_mcp') IS NULL
    CREATE LOGIN ari_mcp WITH PASSWORD = N'CHANGE_ME_Str0ng!Pass', DEFAULT_DATABASE = ArisMemory, CHECK_POLICY = ON;
GO

USE ArisMemory;
GO

IF USER_ID(N'ari_mcp') IS NULL
    CREATE USER ari_mcp FOR LOGIN ari_mcp;
GO

GRANT SELECT, INSERT ON dbo.Memories TO ari_mcp;
GRANT UPDATE ON dbo.Memories (Topic, Content, UpdatedAt, DeletedAt) TO ari_mcp;
DENY DELETE ON dbo.Memories TO ari_mcp;
GO
