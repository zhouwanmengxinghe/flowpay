import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

class Healthcheck {
    public static void main(String[] args) throws Exception {
        var response = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build()
            .send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:8080/api/health"))
                .timeout(Duration.ofSeconds(3)).GET().build(), HttpResponse.BodyHandlers.discarding());
        if (response.statusCode() != 200) System.exit(1);
    }
}
