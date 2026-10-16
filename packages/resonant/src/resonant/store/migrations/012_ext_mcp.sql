-- 012: MCP servers as extension tools (docs/dashboard-plan.md MCP-1). What `ext add` approved
-- for each MCP tool: {tool: {server, pin, input_schema}}. The pin is
-- sha256(name, description, input_schema, output_schema, annotations) as the server listed it;
-- input_schema is the stripped schema the model sees. The host compares live tools to it.
ALTER TABLE extensions ADD COLUMN mcp_pins TEXT NOT NULL DEFAULT '{}';
