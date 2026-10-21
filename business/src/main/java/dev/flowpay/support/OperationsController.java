package dev.flowpay.support;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import jakarta.validation.constraints.*;
import java.util.Map;
import io.micrometer.prometheusmetrics.PrometheusMeterRegistry;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api")
public class OperationsController {
    public record Member(@NotBlank @Pattern(regexp="[A-Za-z0-9_]{3,60}") String username,@NotBlank @Size(min=12,max=72) String password,boolean administrator) {}
    public record MemberState(@NotNull Boolean active,@NotNull Boolean administrator) {}
    public record Grant(@NotBlank String userId,@NotBlank String tenantId,@NotNull Boolean enabled) {}
    public record Limits(@Min(1) @Max(600) int requestsPerMinute,@Min(1) @Max(10000) int dailyRuns,@Min(1) @Max(8) int activeRuns) {}
    private final Administration admin; private final Identity identity; private final JdbcTemplate db;
    private final AgentGateway agent; private final PrometheusMeterRegistry metrics;
    private final RunCoordinator coordinator;
    public OperationsController(Administration admin,Identity identity,JdbcTemplate db,AgentGateway agent,
                                PrometheusMeterRegistry metrics,RunCoordinator coordinator) {
        this.admin=admin; this.identity=identity; this.db=db; this.agent=agent; this.metrics=metrics; this.coordinator=coordinator;
    }
    private Identity.User user(HttpServletRequest req) { return (Identity.User)req.getAttribute("user"); }
    @GetMapping("/admin/capabilities") Object capabilities(HttpServletRequest req) { return identity.permissions(user(req)); }
    @GetMapping("/agent/status") Object agentStatus() {
        var status=agent.status();
        return Map.of("status",status.get("status"),"mode",status.get("mode"),"engine",status.get("engine"));
    }
    @GetMapping("/members") Object members(HttpServletRequest req) { return admin.members(user(req)); }
    @PostMapping("/members") Object create(HttpServletRequest req,@Valid @RequestBody Member body) { return admin.createMember(user(req),body.username(),body.password(),body.administrator()); }
    @PostMapping("/members/{id}") Object update(HttpServletRequest req,@PathVariable String id,@Valid @RequestBody MemberState body) { admin.updateMember(user(req),id,body.active(),body.administrator()); return Map.of("ok",true); }
    @GetMapping("/admin/grants") Object grants(HttpServletRequest req) { return admin.grants(user(req)); }
    @PostMapping("/admin/grants") Object grant(HttpServletRequest req,@Valid @RequestBody Grant body) { admin.grant(user(req),body.userId(),body.tenantId(),body.enabled()); return Map.of("ok",true); }
    @GetMapping("/admin/audit") Object audit(HttpServletRequest req) { return admin.audit(user(req)); }
    @GetMapping("/usage") Object usage(HttpServletRequest req) {
        var u=user(req);
        if(!u.role().equals("MERCHANT")) throw new ApiError(403,"仅商户可查看本商户用量");
        return Map.of("policy",db.queryForMap("SELECT * FROM tenant_limits WHERE tenant_id=?",u.tenantId()),
            "today",db.queryForObject("SELECT count(*) FROM runs r JOIN sessions s ON s.id=r.session_id WHERE s.tenant_id=? AND r.created_at >= date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",Long.class,u.tenantId()));
    }
    @GetMapping("/admin/limits") Object limits(HttpServletRequest req) {
        identity.require(user(req),"SUPPORT_ADMIN");
        return db.queryForList("SELECT t.id AS tenant_id,t.name,l.requests_per_minute,l.daily_runs,l.active_runs FROM tenants t JOIN tenant_limits l ON l.tenant_id=t.id ORDER BY t.id");
    }
    @PostMapping("/admin/limits/{tenant}") Object limits(HttpServletRequest req,@PathVariable String tenant,@Valid @RequestBody Limits body) {
        admin.limits(user(req),tenant,body.requestsPerMinute(),body.dailyRuns(),body.activeRuns());
        return Map.of("ok",true);
    }
    @GetMapping("/operations") Object operations(HttpServletRequest req) {
        identity.require(user(req),"OBSERVE");
        return Map.of("runs",db.queryForList("SELECT execution_mode,execution_engine,status,count(*) AS count FROM runs GROUP BY execution_mode,execution_engine,status"),
            "latency",db.queryForList("SELECT execution_mode,COALESCE(avg(extract(epoch FROM ended_at-created_at)),0) AS mean_seconds, COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM ended_at-created_at)),0) AS p95_seconds FROM runs WHERE ended_at IS NOT NULL AND created_at>now()-interval '1 day' GROUP BY execution_mode"),
            "agentHealthy",agent.healthy(),"workerActive",agent.activeCount(),"scheduler",coordinator.status());
    }
    @GetMapping(value="/operations/prometheus",produces="text/plain; version=0.0.4") String prometheus(HttpServletRequest req) { identity.require(user(req),"OBSERVE"); return metrics.scrape(); }
}
