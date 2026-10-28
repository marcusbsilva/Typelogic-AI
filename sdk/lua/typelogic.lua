-- TypeLogic AI for Lua 5.4: wrappers over the Rust core module (typelogic_core.so / typelogic_core.dll).
-- Pure Lua, no third-party libraries. JSON null is `typelogic.null`; mark an empty array with `typelogic.array{}`.
local core = require "typelogic_core"

local M = { null = setmetatable({}, { __tostring = function() return "null" end }), core = core }
local ARRAY = {}

function M.array(t) return setmetatable(t or {}, ARRAY) end

-- ------------------------------------------------------------------ JSON encoding
local escapes = { ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }

local function encode_string(s)
  if not utf8.len(s) then error("string is not valid UTF-8", 3) end
  return '"' .. s:gsub('[%c"\\]', function(c) return escapes[c] or string.format('\\u%04x', c:byte()) end) .. '"'
end

local function is_array(t)
  if getmetatable(t) == ARRAY then return true end
  local n = 0
  for k in pairs(t) do
    if math.type(k) ~= "integer" or k < 1 then return false end
    n = n + 1
  end
  return n > 0 and n == #t
end

local encode
function encode(value, depth)
  depth = depth or 0
  if depth > 200 then error("nesting too deep", 2) end
  local kind = type(value)
  if value == nil or value == M.null then return "null"
  elseif kind == "boolean" then return value and "true" or "false"
  elseif kind == "number" then
    if math.type(value) == "integer" then return tostring(value) end
    if value ~= value or value == math.huge or value == -math.huge then error("JSON must contain finite values", 2) end
    local text = string.format("%.17g", value)
    if not text:find("[%.eEn]") then text = text .. ".0" end -- keep floats distinct from integers
    return text
  elseif kind == "string" then return encode_string(value)
  elseif kind == "table" then
    local parts = {}
    if is_array(value) then
      for i = 1, #value do parts[i] = encode(value[i], depth + 1) end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    local keys = {}
    for k in pairs(value) do
      if type(k) ~= "string" then error("object keys must be strings", 2) end
      keys[#keys + 1] = k
    end
    table.sort(keys)
    for i, k in ipairs(keys) do parts[i] = encode_string(k) .. ":" .. encode(value[k], depth + 1) end
    return "{" .. table.concat(parts, ",") .. "}"
  end
  error("cannot encode " .. kind, 2)
end
M.encode = encode

-- ------------------------------------------------------------------ JSON decoding
local function decode(text)
  local pos = 1
  local function fail(msg) error(string.format("JSON %s at byte %d", msg, pos), 0) end
  local function ws() pos = text:find("[^ \t\r\n]", pos) or #text + 1 end
  local value
  local function str()
    local out, i = {}, pos + 1
    while true do
      local j = text:find('["\\]', i)
      if not j then fail("unterminated string") end
      out[#out + 1] = text:sub(i, j - 1)
      if text:sub(j, j) == '"' then pos = j + 1; return table.concat(out) end
      local e = text:sub(j + 1, j + 1)
      local simple = ({ ['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b', f = '\f', n = '\n', r = '\r', t = '\t' })[e]
      if simple then out[#out + 1] = simple; i = j + 2
      elseif e == "u" then
        local cp = tonumber(text:sub(j + 2, j + 5), 16) or fail("bad \\u escape")
        i = j + 6
        if cp >= 0xD800 and cp <= 0xDBFF and text:sub(i, i + 1) == "\\u" then
          local lo = tonumber(text:sub(i + 2, i + 5), 16)
          if lo and lo >= 0xDC00 and lo <= 0xDFFF then cp = 0x10000 + (cp - 0xD800) * 0x400 + (lo - 0xDC00); i = i + 6 end
        end
        out[#out + 1] = utf8.char(cp)
      else fail("bad escape") end
    end
  end
  function value()
    ws()
    local c = text:sub(pos, pos)
    if c == "{" then
      pos = pos + 1; local obj = {}; ws()
      if text:sub(pos, pos) == "}" then pos = pos + 1; return obj end
      while true do
        ws(); if text:sub(pos, pos) ~= '"' then fail("expected key") end
        local k = str(); ws()
        if text:sub(pos, pos) ~= ":" then fail("expected ':'") end
        pos = pos + 1; obj[k] = value(); ws()
        local d = text:sub(pos, pos); pos = pos + 1
        if d == "}" then return obj elseif d ~= "," then fail("expected ',' or '}'") end
      end
    elseif c == "[" then
      pos = pos + 1; local arr = M.array({}); ws()
      if text:sub(pos, pos) == "]" then pos = pos + 1; return arr end
      while true do
        arr[#arr + 1] = value(); ws()
        local d = text:sub(pos, pos); pos = pos + 1
        if d == "]" then return arr elseif d ~= "," then fail("expected ',' or ']'") end
      end
    elseif c == '"' then return str()
    elseif text:sub(pos, pos + 3) == "true" then pos = pos + 4; return true
    elseif text:sub(pos, pos + 4) == "false" then pos = pos + 5; return false
    elseif text:sub(pos, pos + 3) == "null" then pos = pos + 4; return M.null
    else
      local num = text:match("^-?%d+%.?%d*[eE]?[-+]?%d*", pos) or fail("unexpected character")
      pos = pos + #num
      return math.tointeger(tonumber(num)) and not num:find("[%.eE]") and math.tointeger(tonumber(num)) or tonumber(num)
    end
  end
  local result = value(); ws()
  if pos <= #text then fail("trailing data") end
  return result
end
M.decode = decode

-- ------------------------------------------------------------------ core calls
--- Call a core method; raises a Lua error "ValueError: message" on failure.
function M.call(method, params)
  local response, err = core.call(method, encode(params or {}))
  if not response then error(err, 2) end
  local decoded = decode(response)
  if decoded.error then error(decoded.error.type .. ": " .. decoded.error.message, 2) end
  return decoded.ok
end

--- Same as M.call but returns (nil, type, message) instead of raising.
function M.try(method, params)
  local ok, result = pcall(M.call, method, params)
  if ok then return result end
  local kind, message = tostring(result):match("^(%w+): (.*)$")
  return nil, kind or "Error", message or tostring(result)
end

function M.version() return core.version() end
function M.validate(payload) return M.call("validate", { payload = payload }) end
function M.answers(questions, rows, threshold, calibration)
  return M.call("answers", { questions = questions, rows = rows, threshold = threshold, calibration = calibration or "unvalidated" })
end
function M.canonical(value, compact) return M.call("canonical", { value = value, compact = compact or false }) end
function M.digests(record) return M.call("digests", { record = record }) end
function M.verification(record) return M.call("verification", { record = record }) end
function M.lite(record) return M.call("lite", { record = record }) end
function M.export_filename(id, format) return M.call("export_filename", { id = id, format = format or "full" }) end
function M.hash(algorithm, text) return M.call("hash", { algorithm = algorithm, text = text }) end
function M.route(request, profiles) return M.call("route", { request = request, profiles = M.array(profiles) }) end

return M
