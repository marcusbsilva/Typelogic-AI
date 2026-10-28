-- Run: lua sdk/lua/test_typelogic.lua  (LUA_CPATH must find typelogic_core.so/.dll; LUA_PATH this folder)
local tl = require "typelogic"
local passed, failed = 0, 0
local function check(name, cond, detail)
  if cond then passed = passed + 1 else failed = failed + 1; print("FAIL " .. name .. (detail and (": " .. detail) or "")) end
end

local here = arg[0]:match("(.*[/\\])") or "./"
local f = assert(io.open(here .. "../testdata/golden.json", "rb")); local golden = tl.decode(f:read("a")); f:close()
for i, c in ipairs(golden.cases) do
  local response = tl.core.call(c.method, c.params_json)
  check("golden " .. i .. " " .. c.method, response == c.response, response and response:sub(1, 160))
end

local v = tl.validate({ state = "Refund?", questions = { r = { type = "choice", instructions = "Route?", criteria = { "A", "B" } } } })
check("validate state", v.state == "Refund?")
check("validate keys", v.questions[1].keys[1] == "A" and v.questions[1].keys[2] == "B")
local a = tl.answers(v.questions, { { 0.25, 0.75 } }, v.threshold)
check("answers choice", a.r.choice == "B" and a.r.eligible_for_automation == false)
check("export filename", tl.export_filename("abc", "lite") == "xe26jdxx.json")
check("canonical", tl.canonical({ b = 1, a = tl.array({ 1.5, 2 }) }) == '{"a": [1.5, 2], "b": 1}')
local ok, err = pcall(tl.validate, { state = " ", questions = {} })
check("error raised", not ok and tostring(err):find("ValueError: Scenario", 1, true) ~= nil, tostring(err))
local none, kind, message = tl.try("validate", { payload = { state = " " } })
check("try returns error", none == nil and kind == "ValueError" and message:find("Scenario") ~= nil)
local r1, m1 = tl.core.call(1, {})
check("bad args return nil,msg", r1 == nil and m1:find("expects two") ~= nil)
check("json roundtrip", tl.encode(tl.decode('{"a":[1,2.5,"é\\n",null,true],"b":{}}')) == '{"a":[1,2.5,"é\\n",null,true],"b":{}}')
print(string.format("%d passed, %d failed (core %s)", passed, failed, tl.version()))
os.exit(failed == 0 and 0 or 1)
