package dev.flowpay.support;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public class Json {
    private final ObjectMapper mapper;
    public Json(ObjectMapper mapper) { this.mapper = mapper; }
    public String write(Object value) {
        try { return mapper.writeValueAsString(value); }
        catch (Exception e) { throw new IllegalArgumentException("JSON serialization failed", e); }
    }
    public Map<String, Object> object(String value) {
        try { return mapper.readValue(value, new TypeReference<>() {}); }
        catch (Exception e) { throw new IllegalArgumentException("Invalid JSON object", e); }
    }
}
