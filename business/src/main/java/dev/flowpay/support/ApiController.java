package dev.flowpay.support;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.availability.ApplicationAvailability;
import org.springframework.boot.availability.ReadinessState;
import org.springframework.http.ResponseCookie;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController @RequestMapping("/api")
public class ApiController {
    public record Login(@NotBlank @Size(max=80) String username,@NotBlank @Size(max=200) String password) {}
    public record NewSession(@NotBlank @Size(max=100) String accountId,@NotBlank String environment) {}
    public record Message(@NotBlank @Size(max=128) String requestId,@NotBlank @Size(max=20000) String content) {}
    public record NewTicket(@NotBlank String sessionId,@NotBlank @Size(max=128) String requestId,@NotBlank @Size(max=160) String title) {}
    public record Transition(@Min(0) int version,@NotBlank String status,@NotBlank @Size(max=2000) String note) {}
    private final Identity identity; private final SupportService sessions; private final TicketService tickets;
    private final boolean secureCookie; private final Json json;
    private final ApplicationAvailability availability; private final SingleInstanceLease lease;
    public ApiController(Identity identity,SupportService sessions,TicketService tickets,Json json,ApplicationAvailability availability,
                         SingleInstanceLease lease,@Value("${support.secure-cookie}") boolean secureCookie) {
        this.identity=identity; this.sessions=sessions; this.tickets=tickets; this.json=json; this.secureCookie=secureCookie;
        this.availability=availability; this.lease=lease;
    }
    private Identity.User user(HttpServletRequest req) { return (Identity.User)req.getAttribute("user"); }
    @GetMapping("/health") ResponseEntity<?> health() {
        if(availability.getReadinessState()!=ReadinessState.ACCEPTING_TRAFFIC)
            return ResponseEntity.status(503).body(Map.of("status","starting"));
        lease.check(); return ResponseEntity.ok(Map.of("status","ok"));
    }
    @PostMapping("/auth/login") Object login(@Valid @RequestBody Login body,HttpServletResponse response) {
        String token=identity.login(body.username(),body.password()); cookie(response,token,8*3600);
        return identity.authenticate(token);
    }
    @PostMapping("/auth/logout") Object logout(HttpServletRequest req,HttpServletResponse res) {
        identity.logout(AuthFilter.token(req)); cookie(res,"",0); return Map.of("ok",true);
    }
    private void cookie(HttpServletResponse response,String value,long age) {
        response.addHeader("Set-Cookie",ResponseCookie.from("support_token",value).httpOnly(true).secure(secureCookie)
            .sameSite("Strict").path("/").maxAge(age).build().toString());
    }
    @GetMapping("/auth/me") Object me(HttpServletRequest req) { return user(req); }
    @GetMapping("/accounts") Object accounts(HttpServletRequest req) { return sessions.accounts(user(req)); }
    @GetMapping("/sessions") Object list(HttpServletRequest req) { return sessions.sessions(user(req)); }
    @PostMapping("/sessions") Object create(HttpServletRequest req,@Valid @RequestBody NewSession body) {
        return sessions.createSession(user(req),body.accountId(),body.environment());
    }
    @GetMapping("/sessions/{id}") Object detail(HttpServletRequest req,@PathVariable String id) { return sessions.detail(user(req),id); }
    @GetMapping("/sessions/{id}/events") Object events(HttpServletRequest req,@PathVariable String id,@RequestParam(defaultValue="0") long after) {
        if(after<0) throw new ApiError(400,"游标不能为负数"); return sessions.events(user(req),id,after);
    }
    @GetMapping("/sessions/{id}/export") ResponseEntity<String> export(HttpServletRequest req,@PathVariable String id) {
        var body=new StringBuilder(); long cursor=0;
        while(true) {
            var batch=sessions.events(user(req),id,cursor);
            for(var item:batch) { body.append(json.write(item)).append('\n'); cursor=((Number)item.get("seq")).longValue(); }
            if(batch.size()<1000) break;
            if(body.length()>8_000_000) throw new ApiError(413,"事件过多，请使用带游标的事件接口分批读取");
        }
        return ResponseEntity.ok().header("Content-Disposition","attachment; filename=session-events.ndjson")
            .header("Content-Type","application/x-ndjson").body(body.toString());
    }
    @PostMapping("/sessions/{id}/messages") ResponseEntity<?> message(HttpServletRequest req,@PathVariable String id,@Valid @RequestBody Message body) {
        return ResponseEntity.accepted().body(sessions.submit(user(req),id,body.requestId(),body.content()));
    }
    @PostMapping("/sessions/{id}/cancel") Object cancel(HttpServletRequest req,@PathVariable String id) {
        sessions.cancel(user(req),id); return sessions.detail(user(req),id);
    }
    @GetMapping("/tickets") Object tickets(HttpServletRequest req) { return tickets.list(user(req)); }
    @PostMapping("/tickets") Object createTicket(HttpServletRequest req,@Valid @RequestBody NewTicket body) {
        return tickets.create(user(req),body.sessionId(),body.requestId(),body.title());
    }
    @GetMapping("/tickets/{id}") Object ticket(HttpServletRequest req,@PathVariable String id) { return tickets.detail(user(req),id); }
    @PostMapping("/tickets/{id}/transition") Object transition(HttpServletRequest req,@PathVariable String id,@Valid @RequestBody Transition body) {
        return tickets.transition(user(req),id,body.version(),body.status(),body.note());
    }
}
