# Agent Policy Service: static binary on distroless nonroot (spec 9.1).
FROM golang:1.24.7-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
ENV CGO_ENABLED=0 GOOS=linux GOTOOLCHAIN=local
RUN go build -trimpath -ldflags="-s -w" -o /out/aps ./cmd/aps

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/aps /aps
USER 65532:65532
EXPOSE 9001 9002 4317 9090 8081
ENTRYPOINT ["/aps"]
