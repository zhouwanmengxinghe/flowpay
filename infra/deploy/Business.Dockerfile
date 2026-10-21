FROM maven:3.9.9-eclipse-temurin-21 AS build
ARG MAVEN_PROXY_HOST=""
ARG MAVEN_PROXY_PORT=7890
ENV MAVEN_OPTS="-XX:ActiveProcessorCount=2 -Xmx512m"
WORKDIR /build
COPY business/pom.xml ./pom.xml
COPY infra/deploy/maven-settings.xml /tmp/maven-settings.template.xml
RUN if [ -n "$MAVEN_PROXY_HOST" ]; then \
      case "$MAVEN_PROXY_HOST" in *[!a-zA-Z0-9.:-]*) echo 'Invalid Maven proxy host' >&2; exit 2;; esac; \
      case "$MAVEN_PROXY_PORT" in ''|*[!0-9]*) echo 'Invalid Maven proxy port' >&2; exit 2;; esac; \
      (test "$MAVEN_PROXY_PORT" -gt 0 && test "$MAVEN_PROXY_PORT" -lt 65536) || exit 2; \
      sed -e "s|__PROXY_HOST__|$MAVEN_PROXY_HOST|g" -e "s|__PROXY_PORT__|$MAVEN_PROXY_PORT|g" \
        /tmp/maven-settings.template.xml > /tmp/maven-settings.xml; \
    fi
COPY business/src ./src
COPY infra/deploy/Healthcheck.java /tmp/Healthcheck.java
RUN --mount=type=cache,target=/root/.m2 \
    if [ -n "$MAVEN_PROXY_HOST" ]; then \
      mvn -B -ntp -s /tmp/maven-settings.xml package -DskipTests; \
    else mvn -B -ntp package -DskipTests; fi
RUN javac -d /build /tmp/Healthcheck.java

FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /build/target/support-business-0.1.0.jar /app/support.jar
COPY --from=build /build/Healthcheck.class /app/Healthcheck.class
COPY README.md /app/README.md
USER 10001
ENTRYPOINT ["java", "-XX:ActiveProcessorCount=2", "-XX:MaxRAMPercentage=70", "-jar", "/app/support.jar"]
