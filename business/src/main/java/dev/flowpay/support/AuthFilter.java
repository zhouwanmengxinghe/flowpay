package dev.flowpay.support;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.Cookie;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.Arrays;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

@Component
public class AuthFilter extends OncePerRequestFilter {
    private final Identity identity;
    private final Json json;
    private final String origin;
    public AuthFilter(Identity identity, Json json, @Value("${support.origin}") String origin) {
        this.identity = identity; this.json = json; this.origin = origin;
    }
    public static String token(HttpServletRequest request) {
        return Arrays.stream(request.getCookies() == null ? new Cookie[0] : request.getCookies())
            .filter(c -> c.getName().equals("support_token")).map(Cookie::getValue).findFirst().orElse(null);
    }
    @Override
    protected void doFilterInternal(HttpServletRequest req, HttpServletResponse res, FilterChain chain) throws ServletException, IOException {
        res.setHeader("X-Content-Type-Options", "nosniff");
        res.setHeader("Referrer-Policy", "same-origin");
        res.setHeader("X-Frame-Options", "DENY");
        res.setHeader("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'");
        if (!req.getRequestURI().startsWith("/api/")) { chain.doFilter(req,res); return; }
        res.setHeader("Cache-Control", "no-store");
        try {
            // SameSite Cookie + Origin/Fetch Metadata，CLI 无 Origin 时仍可使用明确 Cookie。
            if ("cross-site".equals(req.getHeader("Sec-Fetch-Site"))) throw new ApiError(403, "跨站请求已拒绝");
            String incoming = req.getHeader("Origin");
            if (incoming != null && !incoming.equals(origin)) throw new ApiError(403, "请求来源不允许");
            String path = req.getRequestURI();
            if (!path.equals("/api/health") && !path.equals("/api/auth/login"))
                req.setAttribute("user", identity.authenticate(token(req)));
            chain.doFilter(req,res);
        } catch (ApiError e) {
            res.setStatus(e.status); res.setContentType("application/json;charset=UTF-8");
            res.getWriter().write(json.write(java.util.Map.of("detail",e.getMessage())));
        }
    }
}
