package dev.flowpay.support;

import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

public class ApiError extends RuntimeException {
    public final int status;
    public ApiError(int status, String message) { super(message); this.status = status; }

    @RestControllerAdvice
    static class Advice {
        @ExceptionHandler(ApiError.class)
        ResponseEntity<?> business(ApiError e) {
            return ResponseEntity.status(e.status).body(Map.of("detail", e.getMessage()));
        }
        @ExceptionHandler(MethodArgumentNotValidException.class)
        ResponseEntity<?> invalid(MethodArgumentNotValidException e) {
            return ResponseEntity.badRequest().body(Map.of("detail", "请求字段不符合要求"));
        }
    }
}
